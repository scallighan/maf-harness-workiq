from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from azure.core import MatchConditions
from azure.cosmos import ContainerProxy
from azure.cosmos.exceptions import (
    CosmosAccessConditionFailedError,
    CosmosResourceExistsError,
    CosmosResourceNotFoundError,
)
from azure.keyvault.keys.crypto import CryptographyClient, KeyWrapAlgorithm
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class StoreBusy(RuntimeError):
    pass


class StoreMissing(RuntimeError):
    pass


class CacheKeyChanged(RuntimeError):
    pass


@dataclass
class CacheLease:
    document: dict[str, Any]
    cache: str | None


class EncryptedTokenStore:
    def __init__(
        self, container: ContainerProxy, crypto: CryptographyClient,
        tenant_id: str, *, cache_ttl: int = 30 * 86400,
    ) -> None:
        self.container = container
        self.crypto = crypto
        self.tenant_id = tenant_id
        self.cache_ttl = cache_ttl

    def key(self, kind: str, subject: str) -> str:
        return hashlib.sha256(
            json.dumps([self.tenant_id, kind, subject], separators=(",", ":")).encode()
        ).hexdigest()

    def _encrypt(self, key: str, plaintext: str) -> dict[str, str]:
        secret, nonce = AESGCM.generate_key(bit_length=256), os.urandom(12)
        wrapped = self.crypto.wrap_key(KeyWrapAlgorithm.rsa_oaep_256, secret)
        if not wrapped.key_id:
            raise ValueError("Key Vault did not return the wrapping key ID.")
        return {
            "key_id": wrapped.key_id,
            "wrapped_key": base64.b64encode(wrapped.encrypted_key).decode(),
            "nonce": base64.b64encode(nonce).decode(),
            "ciphertext": base64.b64encode(AESGCM(secret).encrypt(
                nonce, plaintext.encode(), key.encode()
            )).decode(),
        }

    def _decrypt(self, key: str, envelope: dict[str, str]) -> str:
        if envelope["key_id"] != self.crypto.key_id:
            raise CacheKeyChanged(
                "The stored OAuth cache uses a different wrapping key; reauthorize after rotation."
            )
        secret = self.crypto.unwrap_key(
            KeyWrapAlgorithm.rsa_oaep_256,
            base64.b64decode(envelope["wrapped_key"], validate=True),
        ).key
        return AESGCM(secret).decrypt(
            base64.b64decode(envelope["nonce"], validate=True),
            base64.b64decode(envelope["ciphertext"], validate=True),
            key.encode(),
        ).decode()

    def put_flow(self, state: str, flow: dict) -> None:
        key = self.key("flow", state)
        self.container.create_item({
            "id": key, "ttl": 600, "expires_at": time.time() + 600,
            "envelope": self._encrypt(key, json.dumps(flow)),
        })

    def consume_flow(self, state: str, cookie_hash: str) -> dict:
        key = self.key("flow", state)
        try:
            document = self.container.read_item(key, partition_key=key)
        except CosmosResourceNotFoundError as error:
            raise StoreMissing("OAuth login has expired or has already been used.") from error
        if document["expires_at"] <= time.time():
            raise StoreMissing("OAuth login has expired.")
        value = json.loads(self._decrypt(key, document["envelope"]))
        if not isinstance(value, dict):
            raise ValueError("The stored OAuth flow is invalid.")
        if not hmac.compare_digest(value.get("cookie_hash", ""), cookie_hash):
            raise StoreMissing("OAuth login does not match this browser.")
        try:
            self.container.delete_item(
                key, partition_key=key, etag=document["_etag"],
                match_condition=MatchConditions.IfNotModified,
            )
        except (CosmosResourceNotFoundError, CosmosAccessConditionFailedError) as error:
            raise StoreMissing("OAuth login has already been used.") from error
        return value

    def _replace(self, document: dict[str, Any], **updates: Any) -> dict[str, Any]:
        body = {key: value for key, value in document.items() if not key.startswith("_")}
        body.update(updates)
        body["ttl"] = max(1, int(max(body["expires_at"], body.get("lock_until", 0)) - time.time()))
        try:
            return self.container.replace_item(
                document["id"], body, etag=document["_etag"],
                match_condition=MatchConditions.IfNotModified,
            )
        except CosmosAccessConditionFailedError as error:
            raise StoreBusy("A concurrent sign-in or token refresh is in progress; retry.") from error

    @contextmanager
    def edit_cache(self, user_oid: str, *, load_cache: bool = True) -> Iterator[CacheLease]:
        key, owner = self.key("cache", user_oid), str(uuid.uuid4())
        now = time.time()
        try:
            document = self.container.read_item(key, partition_key=key)
        except CosmosResourceNotFoundError:
            try:
                document = self.container.create_item({
                    "id": key, "ttl": self.cache_ttl, "expires_at": now + self.cache_ttl,
                    "lock_owner": owner, "lock_until": now + 120,
                })
            except CosmosResourceExistsError as error:
                raise StoreBusy("A concurrent sign-in is in progress; retry.") from error
        else:
            if document.get("lock_until", 0) > now:
                raise StoreBusy("A concurrent sign-in or token refresh is in progress; retry.")
            updates: dict[str, Any] = {"lock_owner": owner, "lock_until": now + 120}
            if document["expires_at"] <= now:
                updates.update(envelope=None, expires_at=now + self.cache_ttl)
            document = self._replace(document, **updates)
        lease = CacheLease(document, None)
        try:
            if load_cache and document.get("envelope") and document["expires_at"] > now:
                lease.cache = self._decrypt(key, document["envelope"])
            yield lease
        finally:
            lease.document = self._replace(lease.document, lock_owner="", lock_until=0)

    def save_cache(self, lease: CacheLease, serialized: str) -> None:
        if lease.document["lock_until"] <= time.time():
            raise StoreBusy("The token refresh lease expired; retry.")
        lease.document = self._replace(
            lease.document, envelope=self._encrypt(lease.document["id"], serialized),
            expires_at=time.time() + self.cache_ttl,
        )
        lease.cache = serialized
