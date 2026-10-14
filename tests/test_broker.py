import copy
import hashlib
import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import jwt
import msal
import pytest
from azure.cosmos.exceptions import (
    CosmosAccessConditionFailedError,
    CosmosResourceExistsError,
    CosmosResourceNotFoundError,
)
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import HTTPException
from fastapi.testclient import TestClient

from workplace_agent.broker import COOKIE, ROLE, Broker, BrokerConfig, create_app
from workplace_agent.token_store import EncryptedTokenStore, StoreBusy, StoreMissing

TENANT = "11111111-1111-4111-8111-111111111111"
CLIENT = "22222222-2222-4222-8222-222222222222"
ALICE = "33333333-3333-4333-8333-333333333333"
BOB = "44444444-4444-4444-8444-444444444444"


class EtagContainer:
    def __init__(self) -> None:
        self.items: dict[str, dict] = {}
        self.version = 0

    def _write(self, document: dict) -> dict:
        self.version += 1
        document = copy.deepcopy(document)
        document["_etag"] = str(self.version)
        self.items[document["id"]] = document
        return copy.deepcopy(document)

    def create_item(self, document: dict) -> dict:
        if document["id"] in self.items:
            raise CosmosResourceExistsError(status_code=409)
        return self._write(document)

    def read_item(self, key: str, *, partition_key: str) -> dict:
        assert key == partition_key
        if key not in self.items:
            raise CosmosResourceNotFoundError(status_code=404)
        return copy.deepcopy(self.items[key])

    def replace_item(self, key: str, body: dict, *, etag: str, match_condition) -> dict:
        if self.items[key]["_etag"] != etag:
            raise CosmosAccessConditionFailedError(status_code=412)
        return self._write(body)

    def delete_item(self, key: str, *, partition_key: str, etag: str, match_condition) -> None:
        if key not in self.items:
            raise CosmosResourceNotFoundError(status_code=404)
        if self.items[key]["_etag"] != etag:
            raise CosmosAccessConditionFailedError(status_code=412)
        del self.items[key]


@pytest.fixture
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def stored(rsa_key):
    container = EtagContainer()
    proxy = MagicMock()
    for name in ["create_item", "read_item", "replace_item", "delete_item"]:
        getattr(proxy, name).side_effect = getattr(container, name)
    crypto = MagicMock(key_id="https://sample.vault.azure.net/keys/oauth-cache/version")
    oaep = padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None)
    crypto.wrap_key.side_effect = lambda algorithm, secret: SimpleNamespace(
        key_id=crypto.key_id, encrypted_key=rsa_key.public_key().encrypt(secret, oaep),
    )
    crypto.unwrap_key.side_effect = lambda algorithm, wrapped: SimpleNamespace(
        key=rsa_key.decrypt(wrapped, oaep),
    )
    store = EncryptedTokenStore(proxy, crypto, TENANT)
    return SimpleNamespace(store=store, container=container, crypto=crypto)


def test_oauth_cache_encryption_and_cross_user_aad_isolation(stored) -> None:
    store = stored.store
    with store.edit_cache(ALICE) as lease:
        assert lease.cache is None
        store.save_cache(lease, '{"refresh_token":"alice-private-refresh"}')
    assert "alice-private-refresh" not in json.dumps(stored.container.items)
    assert ALICE not in json.dumps(stored.container.items)
    with store.edit_cache(ALICE) as lease:
        assert json.loads(lease.cache)["refresh_token"] == "alice-private-refresh"
    with store.edit_cache(BOB) as lease:
        assert lease.cache is None
    alice_key, bob_key = store.key("cache", ALICE), store.key("cache", BOB)
    stored.container.items[bob_key]["envelope"] = stored.container.items[alice_key]["envelope"]
    with pytest.raises(InvalidTag), store.edit_cache(BOB):
        pytest.fail("A cache encrypted for Alice must not be readable in Bob's partition.")
    assert stored.container.items[bob_key]["lock_until"] == 0


def test_cache_lease_serializes_refresh_and_recovers_from_failure(stored) -> None:
    store = stored.store
    with store.edit_cache(ALICE) as lease:
        with pytest.raises(StoreBusy), store.edit_cache(ALICE):
            pytest.fail("Concurrent refresh must not overwrite a rotated refresh token.")
        store.save_cache(lease, "latest")
    with pytest.raises(RuntimeError, match="refresh failed"), store.edit_cache(ALICE):
        raise RuntimeError("refresh failed")
    with store.edit_cache(ALICE) as lease:
        assert lease.cache == "latest"
    key = store.key("cache", ALICE)
    stored.container.items[key].update(lock_until=time.time() - 1, lock_owner="dead-worker")
    with store.edit_cache(ALICE) as lease:
        assert lease.cache == "latest"
    stored.container.items[key]["expires_at"] = time.time() - 1
    with store.edit_cache(ALICE) as lease:
        assert lease.cache is None
    assert stored.container.items[key]["envelope"] is None


def test_flow_cookie_binding_expiry_and_single_use(stored) -> None:
    store = stored.store
    flow = {"flow": {"state": "random-state", "code_verifier": "private-verifier"}, "cookie_hash": "hash"}
    store.put_flow("random-state", flow)
    assert "private-verifier" not in json.dumps(stored.container.items)
    with pytest.raises(StoreMissing, match="browser"):
        store.consume_flow("random-state", "wrong-browser")
    assert store.consume_flow("random-state", "hash") == flow
    with pytest.raises(StoreMissing, match="already"):
        store.consume_flow("random-state", "hash")
    store.put_flow("expired-state", flow)
    stored.container.items[store.key("flow", "expired-state")]["expires_at"] = time.time() - 1
    with pytest.raises(StoreMissing, match="expired"):
        store.consume_flow("expired-state", "hash")


@pytest.fixture
def authority(stored, rsa_key):
    keys = MagicMock()
    keys.get_signing_key_from_jwt.return_value = SimpleNamespace(key=rsa_key.public_key())
    broker = Broker(
        BrokerConfig(
            TENANT, CLIENT, "https://broker.example.com", {"opaque-alice": ALICE, "opaque-bob": BOB},
        ),
        stored.store, MagicMock(), keys,
    )
    return broker


def service_token(rsa_key, **changes) -> str:
    claims = {
        "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0",
        "aud": CLIENT, "tid": TENANT, "oid": "agent-instance-principal", "azp": "agent-instance-client",
        "idtyp": "app", "roles": [ROLE],
        "exp": int(time.time()) + 3600, "iat": int(time.time()), "nbf": int(time.time()) - 1,
    }
    claims.update(changes)
    return jwt.encode(claims, rsa_key, algorithm="RS256", headers={"kid": "test"})


@pytest.mark.parametrize("changes,status", [
    ({"aud": "another-app"}, 401),
    ({"iss": "https://attacker.example.com"}, 401),
    ({"tid": "another-tenant"}, 403),
    ({"idtyp": "user"}, 403),
    ({"scp": "user-delegation"}, 403),
    ({"roles": []}, 403),
    ({"exp": 1}, 401),
    ({"nbf": int(time.time()) + 3600}, 401),
])
def test_token_endpoint_rejects_untrusted_or_user_tokens(
    authority, rsa_key, changes: dict, status: int,
) -> None:
    authority.user_token = MagicMock()
    with TestClient(create_app(authority), base_url=authority.config.origin) as client:
        response = client.post("/token", json={"user_id": "opaque-alice"},
                               headers={"Authorization": f"Bearer {service_token(rsa_key, **changes)}"})
        assert response.status_code == status
        assert response.headers["Cache-Control"] == "no-store"
    authority.user_token.assert_not_called()


def test_broker_rejects_unsigned_tampered_and_anonymous_tokens(authority, rsa_key) -> None:
    authority.user_token = MagicMock()
    signed = service_token(rsa_key)
    with TestClient(create_app(authority), base_url=authority.config.origin) as client:
        for authorization in ["", f"Bearer {signed[:-10]}aaaaaaaaaa", "Bearer malformed"]:
            response = client.post("/token", json={"user_id": "opaque-alice"},
                                   headers={"Authorization": authorization})
            assert response.status_code == 401
    authority.user_token.assert_not_called()


def fake_application(broker: Broker, *, login_oid: str = ALICE, login_tenant: str = TENANT):
    applications = []

    def factory(cache: msal.SerializableTokenCache):
        application = MagicMock()
        application.initiate_auth_code_flow.return_value = {
            "state": "random-state", "auth_uri": "https://login.microsoftonline.com/authorize",
            "nonce": "nonce", "code_verifier": "private-verifier",
        }

        def exchange(flow, response):
            if response.get("code") == "bad-nonce":
                raise ValueError("Nonce mismatch")
            cache.deserialize(json.dumps({"test_identity": login_oid, "refresh_token": "private-refresh"}))
            return {
                "access_token": f"workiq-{login_oid}", "expires_in": 3600,
                "id_token_claims": {"tid": login_tenant, "oid": login_oid},
            }

        application.acquire_token_by_auth_code_flow.side_effect = exchange
        identity = json.loads(cache.serialize()).get("test_identity")
        application.get_accounts.return_value = [{"local_account_id": identity}] if identity else []
        application.acquire_token_silent.return_value = {
            "access_token": f"workiq-{identity}", "expires_in": 3600,
            "id_token_claims": {"tid": TENANT, "oid": identity},
        }
        applications.append(application)
        return application

    broker.application = factory
    return applications


def test_oauth_browser_login_and_token_endpoint_are_user_bound(authority, rsa_key, stored) -> None:
    applications = fake_application(authority)
    with TestClient(create_app(authority), base_url=authority.config.origin) as client:
        login = client.get("/login", follow_redirects=False)
        assert login.status_code == 303
        assert COOKIE in login.headers["set-cookie"]
        assert all(flag in login.headers["set-cookie"] for flag in ["Secure", "HttpOnly", "SameSite=lax"])
        assert "private-verifier" not in login.text
        callback = client.get("/callback?state=random-state&code=authorization-code")
        assert callback.status_code == 200
        assert "private-refresh" not in callback.text
        assert "no-referrer" == callback.headers["Referrer-Policy"]
        applications[1].acquire_token_by_auth_code_flow.assert_called_once()
        arguments = applications[1].acquire_token_by_auth_code_flow.call_args.args
        assert arguments[0]["nonce"] == "nonce"
        assert arguments[0]["code_verifier"] == "private-verifier"
        headers = {"Authorization": f"Bearer {service_token(rsa_key)}"}
        alice = client.post("/token", json={"user_id": "opaque-alice"}, headers=headers)
        assert alice.status_code == 200
        assert alice.json()["access_token"] == f"workiq-{ALICE}"
        assert client.post("/token", json={"user_id": "opaque-bob"}, headers=headers).status_code == 409
        assert client.post("/token", json={"user_id": "unknown"}, headers=headers).status_code == 409
        assert client.get("/callback?state=random-state&code=replayed").status_code == 400
    assert "private-refresh" not in json.dumps(stored.container.items)


@pytest.mark.parametrize("kind", ["cookie", "nonce", "tenant", "unbound-user"])
def test_failed_oauth_validation_never_persists_user_credentials(authority, stored, kind: str) -> None:
    fake_application(
        authority, login_oid="unapproved" if kind == "unbound-user" else ALICE,
        login_tenant="wrong-tenant" if kind == "tenant" else TENANT,
    )
    with TestClient(create_app(authority), base_url=authority.config.origin) as client:
        client.get("/login", follow_redirects=False)
        if kind == "cookie":
            client.cookies.clear()
        code = "bad-nonce" if kind == "nonce" else "code"
        response = client.get(f"/callback?state=random-state&code={code}")
        assert response.status_code in (400, 403)
    assert not any("lock_owner" in doc for doc in stored.container.items.values())


def test_cached_account_mismatch_and_consent_loss_do_not_return_any_other_user_token(
    authority, stored,
) -> None:
    with stored.store.edit_cache(ALICE) as lease:
        stored.store.save_cache(lease, json.dumps({"test_identity": BOB}))
    fake_application(authority)
    with pytest.raises(HTTPException) as error:
        authority.user_token("opaque-alice")
    assert error.value.status_code == 409


def test_login_cookie_hash_is_not_the_browser_secret(authority, stored) -> None:
    fake_application(authority)
    _, cookie = authority.begin_login()
    assert cookie not in json.dumps(stored.container.items)
    flow = stored.store.consume_flow("random-state", hashlib.sha256(cookie.encode()).hexdigest())
    assert flow["cookie_hash"] != cookie


def test_wrapping_key_rotation_requires_and_allows_fresh_authorization(authority, rsa_key, stored) -> None:
    fake_application(authority)
    with TestClient(create_app(authority), base_url=authority.config.origin) as client:
        client.get("/login", follow_redirects=False)
        assert client.get("/callback?state=random-state&code=first").status_code == 200
        stored.crypto.key_id = "https://sample.vault.azure.net/keys/oauth-cache/rotated-version"
        headers = {"Authorization": f"Bearer {service_token(rsa_key)}"}
        assert client.post("/token", json={"user_id": "opaque-alice"}, headers=headers).status_code == 409
        client.get("/login", follow_redirects=False)
        assert client.get("/callback?state=random-state&code=fresh").status_code == 200
        response = client.post("/token", json={"user_id": "opaque-alice"}, headers=headers)
        assert response.status_code == 200
        assert response.json()["access_token"] == f"workiq-{ALICE}"
