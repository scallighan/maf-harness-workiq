from __future__ import annotations

import json
import re
from urllib.parse import parse_qsl, urlsplit

DISPLAY_LIMIT = 64_000
AUTH_FIELDS = {
    "authorization", "accesstoken", "refreshtoken", "idtoken", "clientsecret", "password",
    "secret", "token", "consentlink", "consenturl", "authorizationurl", "previewurl",
    "microsoftgraphdownloadurl", "apikey", "xapikey", "xmsapikey", "ocpapimsubscriptionkey",
    "cookie", "setcookie", "connectionstring", "privatekey",
}
URL_TOKEN_FIELDS = {
    "code", "access_token", "refresh_token", "id_token", "token", "sig", "signature", "authkey",
    "client_secret",
}
URLS = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
BEARER = re.compile(r"\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/\-=]+", re.IGNORECASE)


def payload_text(value: object) -> tuple[str, bool]:
    truncated = False
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            pass

    def safe_url(match: re.Match) -> str:
        try:
            parsed = urlsplit(match.group(0))
        except ValueError:
            return "[invalid URL omitted]"
        hostname = parsed.hostname or ""
        if (
            hostname in {"login.microsoftonline.com", "consent.azure-apim.net"}
            or hostname.endswith(".consent.azure-apim.net")
            or parsed.username or parsed.password
            or any(
                key.lower() in URL_TOKEN_FIELDS
                for key, _ in parse_qsl(parsed.query) + parse_qsl(parsed.fragment)
            )
            or any(part in parsed.path.lower() for part in ("/oauth/", "/consent", "/authorize"))
        ):
            return "[redacted authentication or temporary URL]"
        return match.group(0)

    def clean(item: object, depth: int = 0) -> object:
        nonlocal truncated
        if depth > 12:
            truncated = True
            return "[nested content omitted]"
        if isinstance(item, dict):
            result = {}
            for index, (key, child) in enumerate(item.items()):
                if index >= 1000:
                    truncated = True
                    result["..."] = "[additional fields omitted]"
                    break
                normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
                result[str(key)] = "[redacted]" if normalized in AUTH_FIELDS else clean(child, depth + 1)
            return result
        if isinstance(item, list):
            if len(item) > 1000:
                truncated = True
            return [clean(child, depth + 1) for child in item[:1000]]
        if isinstance(item, str):
            if len(item) > DISPLAY_LIMIT:
                truncated = True
                item = item[:DISPLAY_LIMIT]
            return BEARER.sub("[redacted credential]", URLS.sub(safe_url, item))
        return item

    safe = clean(value)
    text = safe if isinstance(safe, str) else json.dumps(safe, indent=2, ensure_ascii=False)
    if len(text) > DISPLAY_LIMIT:
        truncated = True
    return text[:DISPLAY_LIMIT], truncated
