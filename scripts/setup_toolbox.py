"""Configure the documented Work IQ OAuth connection and publish its Foundry Toolbox."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import WorkIQPreviewToolboxTool
from azure.core.exceptions import ResourceNotFoundError
from azure.identity import AzureCliCredential

ROOT = Path(__file__).resolve().parents[1]
WORKIQ_A2A_ENDPOINT = "https://workiq.svc.cloud.microsoft/a2a/"
WORKIQ_SCOPE = "api://workiq.svc.cloud.microsoft/WorkIQAgent.Ask"


def terraform_output(name: str) -> str:
    return subprocess.check_output(
        ["terraform", "-chdir=terraform", "output", "-raw", name], cwd=ROOT, text=True,
    ).strip()


@dataclass(frozen=True)
class SetupConfig:
    project_endpoint: str
    project_id: str
    tenant_id: str
    client_id: str
    application_object_id: str
    toolbox_name: str

    @property
    def connection_id(self) -> str:
        return f"{self.project_id}/connections/workiq-toolbox"


def ensure_connection(
    config: SetupConfig, credential: AzureCliCredential, client: httpx.Client,
) -> str:
    arm_url = f"https://management.azure.com{config.connection_id}?api-version=2025-04-01-preview"
    arm_headers = {
        "Authorization": f"Bearer {credential.get_token('https://management.azure.com/.default').token}",
    }
    response = client.get(arm_url, headers=arm_headers)
    if response.status_code == 404:
        graph_headers = {
            "Authorization": f"Bearer {credential.get_token('https://graph.microsoft.com/.default').token}",
        }
        application_url = f"https://graph.microsoft.com/v1.0/applications/{config.application_object_id}"
        password = client.post(
            f"{application_url}/addPassword", headers=graph_headers,
            json={"passwordCredential": {
                "displayName": "Foundry Work IQ Toolbox",
                "endDateTime": (datetime.now(UTC) + timedelta(days=90)).isoformat(),
            }},
        )
        password.raise_for_status()
        password_data = password.json()
        secret = password_data["secretText"]
        key_id = password_data["keyId"]
        properties = {
            "authType": "OAuth2",
            "group": "ServicesAndApps",
            "category": "RemoteA2A",
            "target": WORKIQ_A2A_ENDPOINT,
            "isSharedToAll": True,
            "AuthorizationUrl": f"https://login.microsoftonline.com/{config.tenant_id}/oauth2/v2.0/authorize",
            "TokenUrl": f"https://login.microsoftonline.com/{config.tenant_id}/oauth2/v2.0/token",
            "RefreshUrl": f"https://login.microsoftonline.com/{config.tenant_id}/oauth2/v2.0/token",
            "Scopes": [WORKIQ_SCOPE, "offline_access"],
            "Credentials": {"ClientId": config.client_id, "ClientSecret": secret},
            "metadata": {
                "ApiType": "Azure", "WorkIQClientId": config.client_id,
                "WorkIQPasswordKeyId": key_id,
            },
        }
        try:
            response = client.put(arm_url, headers=arm_headers, json={"properties": properties})
            response.raise_for_status()
        except httpx.HTTPError:
            cleanup = client.post(
                f"{application_url}/removePassword", headers=graph_headers, json={"keyId": key_id},
            )
            cleanup.raise_for_status()
            raise
        print("Created Work IQ OAuth connection; its client secret is stored only in Foundry.")
    else:
        response.raise_for_status()
        print("Reusing the existing Work IQ OAuth connection.")

    properties = response.json()["properties"]
    if (
        properties.get("target") != WORKIQ_A2A_ENDPOINT
        or properties.get("authType") != "OAuth2"
        or properties.get("metadata", {}).get("WorkIQClientId") != config.client_id
    ):
        raise RuntimeError("Existing Work IQ connection does not match this stack's dedicated OAuth app.")
    redirect = properties.get("redirectUrl")
    if not isinstance(redirect, str):
        raise RuntimeError("Foundry did not return the Work IQ connection's OAuth redirect URL.")
    parsed = urlsplit(redirect)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise RuntimeError("Foundry returned an invalid OAuth redirect URL.")
    return redirect


def configure_redirect(
    config: SetupConfig, redirect: str, credential: AzureCliCredential,
    client: httpx.Client, inputs_path: Path,
) -> None:
    response = client.patch(
        f"https://graph.microsoft.com/v1.0/applications/{config.application_object_id}",
        headers={"Authorization": f"Bearer {credential.get_token('https://graph.microsoft.com/.default').token}"},
        json={"web": {"redirectUris": [redirect]}},
    )
    response.raise_for_status()
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=inputs_path.parent, prefix=".toolbox-", delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(f"workiq_toolbox_redirect_uri = {json.dumps(redirect)}\n")
        os.replace(temporary_path, inputs_path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def ensure_toolbox(config: SetupConfig, project: AIProjectClient) -> str:
    try:
        toolbox = project.toolboxes.get(config.toolbox_name)
    except ResourceNotFoundError:
        version = project.toolboxes.create_version(
            name=config.toolbox_name,
            description="Work IQ through the documented Foundry Toolbox integration.",
            tools=[WorkIQPreviewToolboxTool(project_connection_id=config.connection_id)],
        )
        print(f"Created Work IQ toolbox {version.name}:{version.version}.")
        return version.version
    version = project.toolboxes.get_version(config.toolbox_name, toolbox.default_version)
    if (
        len(version.tools) != 1
        or not isinstance(version.tools[0], WorkIQPreviewToolboxTool)
        or version.tools[0].project_connection_id != config.connection_id
    ):
        raise RuntimeError(
            "Existing toolbox configuration differs; review it before changing its default version."
        )
    print(f"Reusing Work IQ toolbox {version.name}:{version.version}.")
    return version.version


def main() -> None:
    config = SetupConfig(
        project_endpoint=terraform_output("foundry_project_endpoint"),
        project_id=terraform_output("foundry_project_id"),
        tenant_id=terraform_output("tenant_id"),
        client_id=terraform_output("workiq_toolbox_client_id"),
        application_object_id=terraform_output("workiq_toolbox_application_object_id"),
        toolbox_name=terraform_output("workiq_toolbox_name"),
    )
    print("Granting the dedicated app's Work IQ delegated admin consent (tenant administrator required).")
    subprocess.run(
        ["az", "ad", "app", "permission", "admin-consent", "--id", config.client_id], check=True,
    )
    with (
        AzureCliCredential() as credential,
        httpx.Client(timeout=60, follow_redirects=False) as client,
        AIProjectClient(endpoint=config.project_endpoint, credential=credential) as project,
    ):
        redirect = ensure_connection(config, credential, client)
        configure_redirect(config, redirect, credential, client, ROOT / "terraform/toolbox.auto.tfvars")
        version = ensure_toolbox(config, project)
    print(
        f"Toolbox ready: {config.project_endpoint}/toolboxes/{config.toolbox_name}"
        f"/versions/{version}/mcp?api-version=v1"
    )
    print("Each user still needs to authorize the Foundry OAuth consent link before using Work IQ.")
    print(
        "The client secret has a 90-day lifetime from creation; "
        "rotate it in Entra and recreate the connection."
    )


if __name__ == "__main__":
    main()
