from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID


@dataclass(frozen=True)
class UIConfig:
    tenant_id: str
    api_client_id: str
    browser_client_id: str
    identity_client_id: str
    origin: str
    project_endpoint: str
    agent_name: str
    cosmos_endpoint: str
    cosmos_database: str
    cosmos_container: str
    static_directory: Path
    additional_origins: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> UIConfig:
        config = cls(
            tenant_id=os.environ["UI_TENANT_ID"],
            api_client_id=os.environ["UI_API_CLIENT_ID"],
            browser_client_id=os.environ["UI_BROWSER_CLIENT_ID"],
            identity_client_id=os.environ["AZURE_CLIENT_ID"],
            origin=os.environ["UI_ORIGIN"].rstrip("/"),
            project_endpoint=os.environ["AZURE_AI_PROJECT_ENDPOINT"].rstrip("/"),
            agent_name=os.environ["UI_AGENT_NAME"],
            cosmos_endpoint=os.environ["COSMOS_ENDPOINT"],
            cosmos_database=os.environ["COSMOS_DATABASE"],
            cosmos_container=os.environ["UI_COSMOS_CONTAINER"],
            static_directory=Path(os.environ.get(
                "UI_STATIC_DIR", str(Path(__file__).resolve().parents[3] / "ui" / "dist"),
            )),
            additional_origins=tuple(filter(None, os.environ.get("UI_ADDITIONAL_ORIGINS", "").split(","))),
        )
        for value in (
            config.tenant_id, config.api_client_id, config.browser_client_id, config.identity_client_id,
        ):
            UUID(value)
        for value in (config.origin, config.project_endpoint, config.cosmos_endpoint):
            parsed = urlsplit(value)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("UI service endpoints must be HTTPS URLs without embedded credentials.")
        if not config.agent_name or any(character in config.agent_name for character in "/?#"):
            raise ValueError("Invalid hosted agent name.")
        for value in config.additional_origins:
            parsed = urlsplit(value)
            if (
                (
                    parsed.scheme != "https"
                    and not (parsed.scheme == "http" and parsed.hostname == "localhost")
                )
                or not parsed.hostname or parsed.username or parsed.password
                or parsed.path or parsed.query or parsed.fragment
            ):
                raise ValueError("Additional UI origins must be HTTPS origins or localhost HTTP origins.")
        return config

    def public(self) -> dict[str, str]:
        return {
            "tenantId": self.tenant_id,
            "clientId": self.browser_client_id,
            "scope": f"api://{self.api_client_id}/access_as_user",
            "agentName": self.agent_name,
        }
