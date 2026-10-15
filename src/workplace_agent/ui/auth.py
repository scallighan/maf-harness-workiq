from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from uuid import UUID

import httpx
import jwt
import msal
from azure.identity import ManagedIdentityCredential
from fastapi import HTTPException

from .config import UIConfig


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    object_id: str
    token: str = field(repr=False)

    def owner(self, config: UIConfig) -> str:
        scope = [
            config.api_client_id, config.project_endpoint, config.agent_name,
            self.tenant_id, self.object_id,
        ]
        return hashlib.sha256(json.dumps(scope, separators=(",", ":")).encode()).hexdigest()


class Authenticator:
    def __init__(self, config: UIConfig) -> None:
        self.config = config
        self.issuer = f"https://login.microsoftonline.com/{config.tenant_id}/v2.0"
        self.keys = jwt.PyJWKClient(
            f"https://login.microsoftonline.com/{config.tenant_id}/discovery/v2.0/keys",
            cache_keys=True, timeout=15,
        )

    def validate(self, token: str) -> Principal:
        try:
            key = self.keys.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token, key, algorithms=["RS256"], audience=self.config.api_client_id,
                issuer=self.issuer,
                options={"require": ["exp", "iat", "iss", "aud", "tid", "oid", "scp", "azp"]},
            )
            if (
                claims["tid"] != self.config.tenant_id
                or claims["azp"] != self.config.browser_client_id
                or "access_as_user" not in claims["scp"].split()
            ):
                raise jwt.InvalidTokenError("Unexpected delegated caller.")
            object_id = str(UUID(claims["oid"]))
        except (jwt.InvalidTokenError, ValueError, TypeError, AttributeError) as error:
            raise HTTPException(
                status_code=401, detail="A valid signed-in UI user is required.",
                headers={"WWW-Authenticate": "Bearer"},
            ) from error
        except jwt.PyJWKClientError as error:
            raise HTTPException(
                status_code=503, detail="Entra signing keys are temporarily unavailable.",
            ) from error
        return Principal(self.config.tenant_id, object_id, token)


class TokenBroker:
    def __init__(self, config: UIConfig) -> None:
        self.credential = ManagedIdentityCredential(client_id=config.identity_client_id)
        self.http = httpx.Client(timeout=20)
        self.application = msal.ConfidentialClientApplication(
            config.api_client_id,
            authority=f"https://login.microsoftonline.com/{config.tenant_id}",
            client_credential={"client_assertion": self.assertion},
            http_client=self.http,
        )

    def assertion(self) -> str:
        return self.credential.get_token("api://AzureADTokenExchange/.default").token

    async def delegated(self, principal: Principal, resource: str) -> str:
        if resource not in {"https://ai.azure.com", "https://graph.microsoft.com"}:
            raise ValueError("Unsupported delegated resource.")
        result = await asyncio.to_thread(
            self.application.acquire_token_on_behalf_of,
            principal.token, scopes=[f"{resource}/.default"],
        )
        token = result.get("access_token")
        if not isinstance(token, str) or not token:
            code = result.get("error", "token_exchange_failed")
            raise HTTPException(
                status_code=403,
                detail=f"Delegated sign-in failed ({code}). Check tenant consent and the user's access.",
            )
        return token

    def close(self) -> None:
        self.http.close()
        self.credential.close()
