from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import AsyncGenerator
from urllib.parse import urlsplit

import httpx
import msal
from azure.core.credentials_async import AsyncTokenCredential

WORKIQ_ENDPOINT = "https://workiq.svc.cloud.microsoft/mcp"
WORKIQ_SCOPES = ["api://workiq.svc.cloud.microsoft/WorkIQAgent.Ask"]
logger = logging.getLogger(__name__)


def redact_tool_error(text: str) -> str:
    text = re.sub(r"(?i)\bbearer\s+[^\s\"']+", "Bearer [redacted]", text)
    text = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", "[redacted]", text)
    text = re.sub(
        r"""(?i)(["']?(?:access_token|refresh_token|id_token|client_secret|authorization|"""
        r"""base64Content|contentBytes)["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;}\]]+)""",
        r"\1[redacted]", text,
    )
    text = re.sub(r"""(https?://)[^\s/@]+@[^\s/?#]+""", r"\1[redacted]", text)
    return re.sub(r"""(https?://[^\s?#"'<>]+)[?#][^\s"'<>]*""", r"\1?[redacted]", text)


def https_origin(url: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
        or parsed.query or parsed.fragment or parsed.path not in ("", "/")
        or parsed.port not in (None, 443)
    ):
        raise ValueError("The token broker must have an HTTPS origin without a path or credentials.")
    return f"https://{parsed.netloc}"


class WorkIQAuthorizationRequired(RuntimeError):
    pass


class WorkIQAuth(httpx.Auth):
    def __init__(self) -> None:
        self._access_token = ""
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> tuple[str, float]:
        raise NotImplementedError

    def redact_tool_error(self, text: str) -> str:
        if self._access_token:
            text = text.replace(self._access_token, "[redacted]")
        return redact_tool_error(text)

    async def async_auth_flow(self, request: httpx.Request) -> AsyncGenerator[httpx.Request, httpx.Response]:
        if request.url.scheme != "https" or request.url.host != "workiq.svc.cloud.microsoft":
            raise ValueError("Work IQ credentials must only be sent to the official HTTPS endpoint.")
        async with self._lock:
            if time.time() >= self._expires_at - 120:
                self._access_token, self._expires_at = await self.acquire()
            request.headers["Authorization"] = f"Bearer {self._access_token}"
        yield request


class LocalWorkIQAuth(WorkIQAuth):
    def __init__(self, tenant_id: str, client_id: str, *, device_code: bool = False) -> None:
        super().__init__()
        self._tenant_id = tenant_id
        self._device_code = device_code
        self._application = msal.PublicClientApplication(
            client_id, authority=f"https://login.microsoftonline.com/{tenant_id}",
            token_cache=msal.SerializableTokenCache(), timeout=30,
        )
        self._account: dict | None = None

    def _acquire(self) -> tuple[str, float]:
        logger.info("Acquiring delegated Work IQ credentials from https://login.microsoftonline.com.")
        interactive = False
        result = (
            self._application.acquire_token_silent(WORKIQ_SCOPES, account=self._account)
            if self._account else None
        )
        if not result or "access_token" not in result:
            interactive = True
            if self._device_code:
                logger.info("Work IQ device-code sign-in required; waiting for user authorization.")
                flow = self._application.initiate_device_flow(scopes=WORKIQ_SCOPES)
                if "user_code" not in flow:
                    raise WorkIQAuthorizationRequired("Work IQ device login could not be started.")
                print(flow["message"], flush=True)
                result = self._application.acquire_token_by_device_flow(flow)
            else:
                logger.info("Opening Work IQ browser sign-in; waiting for user authorization.")
                result = self._application.acquire_token_interactive(scopes=WORKIQ_SCOPES)
        if not result or "access_token" not in result:
            raise WorkIQAuthorizationRequired(
                "Work IQ sign-in/consent failed. "
                "Check tenant enablement, admin consent and Conditional Access."
            )
        claims = result.get("id_token_claims")
        if self._account is None or interactive or claims is not None:
            if not isinstance(claims, dict) or claims.get("tid") != self._tenant_id or not claims.get("oid"):
                raise WorkIQAuthorizationRequired("Work IQ sign-in returned an unexpected user or tenant.")
            if self._account is not None and claims["oid"] != self._account["local_account_id"]:
                raise WorkIQAuthorizationRequired(
                    "Sign in as the original user. "
                    "Restart the demo to switch accounts and discard its history."
                )
            accounts = [
                account for account in self._application.get_accounts()
                if account.get("local_account_id") == claims["oid"]
            ]
            if len(accounts) != 1:
                raise WorkIQAuthorizationRequired("Could not resolve the signed-in Work IQ account.")
            self._account = accounts[0]
        logger.info(
            "Work IQ authorization ready (%s).", "interactive sign-in" if interactive else "cached account",
        )
        return result["access_token"], time.time() + int(result["expires_in"])

    async def acquire(self) -> tuple[str, float]:
        return await asyncio.to_thread(self._acquire)


class BrokerWorkIQAuth(WorkIQAuth):
    def __init__(
        self, origin: str, audience: str, user_id: str,
        credential: AsyncTokenCredential, client: httpx.AsyncClient,
    ) -> None:
        super().__init__()
        self._origin = https_origin(origin)
        self._audience = audience
        self._user_id = user_id
        self._credential = credential
        self._client = client

    async def acquire(self) -> tuple[str, float]:
        identity = await self._credential.get_token(f"{self._audience}/.default")
        attempt = 0
        while True:
            response = await self._client.post(
                f"{self._origin}/token",
                headers={"Authorization": f"Bearer {identity.token}"},
                json={"user_id": self._user_id},
            )
            if response.status_code != 423 or attempt == 2:
                break
            attempt += 1
            await asyncio.sleep(2)
        if response.status_code == 409:
            raise WorkIQAuthorizationRequired(
                f"Sign in to Work IQ at {self._origin}/login, then retry. "
                "An administrator must bind your Foundry caller ID to your Entra user first."
            )
        response.raise_for_status()
        payload = response.json()
        token, expiry = payload.get("access_token"), payload.get("expires_at")
        if not isinstance(token, str) or not token or not isinstance(expiry, (int, float)):
            raise ValueError("The token broker returned an invalid Work IQ credential.")
        if expiry <= time.time():
            raise WorkIQAuthorizationRequired("The token broker returned an expired Work IQ credential.")
        return token, float(expiry)
