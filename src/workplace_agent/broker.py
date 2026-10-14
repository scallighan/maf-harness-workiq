from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import time
import uuid
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import jwt
import msal
import uvicorn
from azure.cosmos import CosmosClient
from azure.identity import ManagedIdentityCredential
from azure.keyvault.keys import KeyClient
from azure.keyvault.keys.crypto import CryptographyClient
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from jwt.exceptions import PyJWKClientConnectionError
from pydantic import BaseModel, Field

from .auth import WORKIQ_SCOPES, https_origin
from .token_store import CacheKeyChanged, EncryptedTokenStore, StoreBusy, StoreMissing

COOKIE = "__Host-workiq-login"
ROLE = "WorkIQ.Token.Read"


@dataclass(frozen=True)
class BrokerConfig:
    tenant_id: str
    client_id: str
    origin: str
    bindings: dict[str, str]

    @classmethod
    def from_env(cls) -> BrokerConfig:
        bindings = json.loads(os.environ.get("FOUNDRY_USER_BINDINGS", "{}"))
        if not isinstance(bindings, dict) or any(
            not isinstance(key, str) or not key or not isinstance(value, str)
            for key, value in bindings.items()
        ):
            raise ValueError(
                "FOUNDRY_USER_BINDINGS must map trusted Foundry caller IDs to Entra user object IDs."
            )
        return cls(
            str(uuid.UUID(os.environ["AZURE_TENANT_ID"])),
            str(uuid.UUID(os.environ["WORKIQ_OAUTH_CLIENT_ID"])),
            https_origin(os.environ["WORKIQ_BROKER_URL"]),
            {key: str(uuid.UUID(value)) for key, value in bindings.items()},
        )


class Broker:
    def __init__(
        self, config: BrokerConfig, store: EncryptedTokenStore,
        credential: ManagedIdentityCredential, jwks: jwt.PyJWKClient,
    ) -> None:
        self.config = config
        self.store = store
        self.credential = credential
        self.jwks = jwks

    def application(self, cache: msal.SerializableTokenCache) -> msal.ConfidentialClientApplication:
        return msal.ConfidentialClientApplication(
            self.config.client_id,
            authority=f"https://login.microsoftonline.com/{self.config.tenant_id}",
            client_credential={"client_assertion": lambda: self.credential.get_token(
                "api://AzureADTokenExchange/.default"
            ).token},
            token_cache=cache, timeout=30,
        )

    def authorize_service(self, authorization: str | None) -> None:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "A broker application token is required.")
        token = authorization.removeprefix("Bearer ")
        try:
            signing_key = self.jwks.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token, signing_key, algorithms=["RS256"], audience=self.config.client_id,
                issuer=f"https://login.microsoftonline.com/{self.config.tenant_id}/v2.0",
                options={"require": ["exp", "iat", "nbf", "tid", "oid", "azp", "idtyp"]},
            )
        except PyJWKClientConnectionError as error:
            raise HTTPException(503, "Broker signing keys are temporarily unavailable.") from error
        except (jwt.InvalidTokenError, jwt.PyJWKClientError) as error:
            raise HTTPException(401, "Invalid broker application token.") from error
        if (
            claims["tid"] != self.config.tenant_id or claims["idtyp"] != "app"
            or "scp" in claims or not isinstance(claims.get("roles"), list)
            or ROLE not in claims["roles"]
        ):
            raise HTTPException(403, "The calling application is not authorized to retrieve Work IQ tokens.")

    def begin_login(self) -> tuple[str, str]:
        flow = self.application(msal.SerializableTokenCache()).initiate_auth_code_flow(
            WORKIQ_SCOPES, redirect_uri=f"{self.config.origin}/callback",
            prompt="select_account",
        )
        if not flow.get("state") or not flow.get("auth_uri"):
            raise RuntimeError("The Work IQ authorization flow could not be initialized.")
        cookie = secrets.token_urlsafe(32)
        self.store.put_flow(flow["state"], {
            "flow": flow, "cookie_hash": hashlib.sha256(cookie.encode()).hexdigest(),
        })
        return flow["auth_uri"], cookie

    def complete_login(self, response: dict[str, str], cookie: str | None) -> None:
        if not cookie or not response.get("state"):
            raise HTTPException(400, "Restart sign-in from /login in the same browser.")
        try:
            stored = self.store.consume_flow(
                response["state"], hashlib.sha256(cookie.encode()).hexdigest(),
            )
        except StoreMissing as error:
            raise HTTPException(400, str(error)) from error
        cache = msal.SerializableTokenCache()
        try:
            result = self.application(cache).acquire_token_by_auth_code_flow(stored["flow"], response)
        except ValueError as error:
            raise HTTPException(
                400, "OAuth state, nonce or callback validation failed; restart sign-in."
            ) from error
        if not result or "access_token" not in result:
            raise HTTPException(403, "Work IQ consent failed. Check admin consent and Conditional Access.")
        claims = result.get("id_token_claims", {})
        oid = claims.get("oid")
        if (
            claims.get("tid") != self.config.tenant_id or not isinstance(oid, str)
            or oid not in self.config.bindings.values()
        ):
            raise HTTPException(403, "This tenant user has no administrator-approved Foundry caller binding.")
        accounts = [account for account in self.application(cache).get_accounts()
                    if account.get("local_account_id") == oid]
        if len(accounts) != 1:
            raise HTTPException(403, "The authorized Work IQ account could not be resolved.")
        with self.store.edit_cache(oid, load_cache=False) as lease:
            self.store.save_cache(lease, cache.serialize())

    def user_token(self, user_id: str) -> dict[str, Any]:
        oid = self.config.bindings.get(user_id)
        if oid is None:
            raise HTTPException(
                409, "Administrator-approved caller binding and Work IQ sign-in are required."
            )
        with self.store.edit_cache(oid) as lease:
            if not lease.cache:
                raise HTTPException(409, "Work IQ sign-in is required.")
            cache = msal.SerializableTokenCache()
            cache.deserialize(lease.cache)
            application = self.application(cache)
            accounts = [account for account in application.get_accounts()
                        if account.get("local_account_id") == oid]
            if len(accounts) != 1:
                raise HTTPException(409, "The user must sign in to Work IQ again.")
            result = application.acquire_token_silent(WORKIQ_SCOPES, account=accounts[0])
            if not result or "access_token" not in result:
                raise HTTPException(409, "Work IQ reauthorization is required; check Conditional Access.")
            claims = result.get("id_token_claims")
            if claims is not None and (
                not isinstance(claims, dict)
                or claims.get("tid") != self.config.tenant_id or claims.get("oid") != oid
            ):
                raise HTTPException(403, "The cached Work IQ identity does not match the invoking user.")
            if cache.has_state_changed:
                self.store.save_cache(lease, cache.serialize())
            return {
                "access_token": result["access_token"],
                "expires_at": time.time() + int(result["expires_in"]),
            }


class TokenRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=512)


def create_app(broker: Broker | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if broker is not None:
            app.state.broker = broker
            app.state.ready = lambda: None
            yield
            return
        with ExitStack() as resources:
            config = BrokerConfig.from_env()
            credential = ManagedIdentityCredential(client_id=os.environ["AZURE_CLIENT_ID"])
            resources.callback(credential.close)
            cosmos = CosmosClient(os.environ["COSMOS_ENDPOINT"], credential=credential)
            resources.callback(cosmos.close)
            container = cosmos.get_database_client(
                os.environ["COSMOS_DATABASE"]
            ).get_container_client(os.environ["COSMOS_TOKEN_CONTAINER"])
            key_url = urlsplit(os.environ["TOKEN_KEY_ID"])
            parts = key_url.path.strip("/").split("/")
            if (
                key_url.scheme != "https" or not key_url.hostname
                or not key_url.hostname.endswith(".vault.azure.net") or len(parts) != 3 or parts[0] != "keys"
            ):
                raise ValueError("TOKEN_KEY_ID must be a versioned Azure Key Vault key URL.")
            keys = KeyClient(f"https://{key_url.netloc}", credential)
            resources.callback(keys.close)
            crypto = CryptographyClient(os.environ["TOKEN_KEY_ID"], credential)
            resources.callback(crypto.close)
            store = EncryptedTokenStore(container, crypto, config.tenant_id)
            jwks = jwt.PyJWKClient(
                f"https://login.microsoftonline.com/{config.tenant_id}/discovery/v2.0/keys",
                timeout=5,
            )
            app.state.broker = Broker(config, store, credential, jwks)

            def ready() -> None:
                container.read()
                keys.get_key(parts[1], parts[2])

            app.state.ready = ready
            yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def protect_responses(request: Request, call_next):
        response = await call_next(request)
        response.headers.update({
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
        })
        return response

    @app.exception_handler(StoreBusy)
    async def busy(request: Request, error: StoreBusy):
        return JSONResponse({"detail": str(error)}, status_code=423, headers={"Retry-After": "2"})

    @app.exception_handler(CacheKeyChanged)
    async def key_changed(request: Request, error: CacheKeyChanged):
        return JSONResponse({"detail": str(error)}, status_code=409)

    @app.get("/readiness")
    def readiness(request: Request):
        request.app.state.ready()
        return {"status": "ready"}

    @app.get("/login")
    def login(request: Request):
        authority: Broker = request.app.state.broker
        url, cookie = authority.begin_login()
        response = RedirectResponse(url, status_code=303)
        response.set_cookie(COOKIE, cookie, max_age=600, secure=True, httponly=True, samesite="lax", path="/")
        return response

    @app.get("/callback")
    def callback(request: Request):
        authority: Broker = request.app.state.broker
        authority.complete_login(dict(request.query_params), request.cookies.get(COOKIE))
        response = HTMLResponse(
            "Work IQ authorization complete. Close this window and retry your agent request."
        )
        response.delete_cookie(COOKIE, secure=True, httponly=True, samesite="lax", path="/")
        return response

    @app.post("/token")
    def token(body: TokenRequest, request: Request):
        authority: Broker = request.app.state.broker
        authority.authorize_service(request.headers.get("Authorization"))
        return authority.user_token(body.user_id)

    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(create_app(), host="0.0.0.0", port=8088, access_log=False, proxy_headers=False)


if __name__ == "__main__":
    main()
