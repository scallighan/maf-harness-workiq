"""Publish generic Work IQ MCP tools using the Terraform-managed OAuth connection."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import MCPToolboxTool
from azure.core.exceptions import ResourceNotFoundError
from azure.identity import AzureCliCredential

ROOT = Path(__file__).resolve().parents[1]
WORKIQ_MCP_ENDPOINT = "https://workiq.svc.cloud.microsoft/mcp"
WORKIQ_SCOPE = "api://workiq.svc.cloud.microsoft/WorkIQAgent.Ask"


def terraform_output(name: str) -> str:
    return subprocess.check_output(
        ["terraform", "-chdir=terraform", "output", "-raw", name], cwd=ROOT, text=True,
    ).strip()


def grant_delegated_consent(client_id: str, credential: AzureCliCredential, client: httpx.Client) -> None:
    headers = {"Authorization": f"Bearer {credential.get_token('https://graph.microsoft.com/.default').token}"}
    base = "https://graph.microsoft.com/v1.0"

    def get(path: str, parameters: dict) -> list[dict]:
        response = client.get(f"{base}/{path}", headers=headers, params=parameters)
        response.raise_for_status()
        return response.json()["value"]

    applications = get("applications", {
        "$filter": f"appId eq '{client_id}'", "$select": "requiredResourceAccess",
    })
    callers = get("servicePrincipals", {"$filter": f"appId eq '{client_id}'", "$select": "id"})
    if len(applications) != 1 or len(callers) != 1:
        raise RuntimeError("The consent application's registration and service principal must exist.")
    caller_id = callers[0]["id"]
    for access in applications[0]["requiredResourceAccess"]:
        if any(permission["type"] != "Scope" for permission in access["resourceAccess"]):
            raise RuntimeError("This setup grants delegated permissions only.")
        resources = get("servicePrincipals", {
            "$filter": f"appId eq '{access['resourceAppId']}'",
            "$select": "id,oauth2PermissionScopes",
        })
        if len(resources) != 1:
            raise RuntimeError("The downstream delegated API service principal must exist.")
        resource = resources[0]
        names = {
            scope["id"]: scope["value"] for scope in resource["oauth2PermissionScopes"]
            if scope["isEnabled"]
        }
        requested = {names[permission["id"]] for permission in access["resourceAccess"]}
        grants = get("oauth2PermissionGrants", {
            "$filter": f"clientId eq '{caller_id}' and resourceId eq '{resource['id']}'",
        })
        existing = [grant for grant in grants if grant["consentType"] == "AllPrincipals"]
        if len(existing) > 1:
            raise RuntimeError("Multiple tenant-wide permission grants require administrator review.")
        if existing:
            grant = existing[0]
            current = set(grant["scope"].split())
            if requested <= current:
                continue
            response = client.patch(
                f"{base}/oauth2PermissionGrants/{grant['id']}", headers=headers,
                json={"scope": " ".join(sorted(current | requested))},
            )
        else:
            response = client.post(f"{base}/oauth2PermissionGrants", headers=headers, json={
                "clientId": caller_id, "resourceId": resource["id"],
                "consentType": "AllPrincipals", "scope": " ".join(sorted(requested)),
            })
        response.raise_for_status()


@dataclass(frozen=True)
class SetupConfig:
    project_endpoint: str
    connection_id: str
    tenant_id: str
    client_id: str
    application_object_id: str
    toolbox_name: str
    allowed_tools: tuple[str, ...]
    require_approval: Literal["always", "never"]


def ensure_connection(
    config: SetupConfig, credential: AzureCliCredential, client: httpx.Client,
) -> str:
    response = client.get(
        f"https://management.azure.com{config.connection_id}?api-version=2025-06-01",
        headers={"Authorization": f"Bearer {credential.get_token('https://management.azure.com/.default').token}"},
    )
    response.raise_for_status()
    properties = response.json()["properties"]
    if (
        properties.get("target") != WORKIQ_MCP_ENDPOINT
        or properties.get("category") != "RemoteTool"
        or properties.get("authType") != "OAuth2"
        or properties.get("authorizationUrl") != (
            f"https://login.microsoftonline.com/{config.tenant_id}/oauth2/v2.0/authorize"
        )
        or properties.get("tokenUrl") != (
            f"https://login.microsoftonline.com/{config.tenant_id}/oauth2/v2.0/token"
        )
        or set(properties.get("scopes", [])) != {WORKIQ_SCOPE, "offline_access"}
    ):
        raise RuntimeError(
            "Terraform's Work IQ connection does not match the expected MCP OAuth configuration."
        )
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
    application_url = f"https://graph.microsoft.com/v1.0/applications/{config.application_object_id}"
    headers = {"Authorization": f"Bearer {credential.get_token('https://graph.microsoft.com/.default').token}"}
    response = client.get(application_url, headers=headers, params={"$select": "web"})
    response.raise_for_status()
    redirects = response.json()["web"]["redirectUris"]
    if not isinstance(redirects, list) or not all(isinstance(uri, str) for uri in redirects):
        raise RuntimeError("Entra returned invalid OAuth redirect URIs.")
    if redirect not in redirects:
        redirects.append(redirect)
        response = client.patch(application_url, headers=headers, json={"web": {"redirectUris": redirects}})
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
    desired = MCPToolboxTool(
        server_label="workiq",
        server_url=WORKIQ_MCP_ENDPOINT,
        project_connection_id=config.connection_id,
        allowed_tools=list(config.allowed_tools),
        require_approval=config.require_approval,
    )
    try:
        toolbox = project.toolboxes.get(config.toolbox_name)
    except ResourceNotFoundError:
        toolbox = None
    if toolbox is not None:
        version = project.toolboxes.get_version(config.toolbox_name, toolbox.default_version)
        if len(version.tools) == 1 and isinstance(version.tools[0], MCPToolboxTool):
            actual = version.tools[0].as_dict()
            if all(actual.get(key) == value for key, value in desired.as_dict().items()):
                print(f"Reusing Work IQ MCP toolbox {version.name}:{version.version}.")
                return version.version
    version = project.toolboxes.create_version(
        name=config.toolbox_name,
        description="Direct Work IQ MCP tools with explicit allowlist and approval policy.",
        tools=[desired],
    )
    if toolbox is not None:
        project.toolboxes.update(config.toolbox_name, default_version=version.version)
    print(f"Published Work IQ MCP toolbox {version.name}:{version.version}.")
    return version.version


def main() -> None:
    approval = terraform_output("workiq_mcp_require_approval")
    if approval != "always" and approval != "never":
        raise RuntimeError("Terraform MCP approval policy must be always or never.")
    allowed_tools = json.loads(subprocess.check_output(
        ["terraform", "-chdir=terraform", "output", "-json", "workiq_mcp_allowed_tools"],
        cwd=ROOT, text=True,
    ))
    config = SetupConfig(
        project_endpoint=terraform_output("foundry_project_endpoint"),
        connection_id=terraform_output("workiq_mcp_connection_id"),
        tenant_id=terraform_output("tenant_id"),
        client_id=terraform_output("workiq_toolbox_client_id"),
        application_object_id=terraform_output("workiq_toolbox_application_object_id"),
        toolbox_name=terraform_output("workiq_toolbox_name"),
        allowed_tools=tuple(allowed_tools),
        require_approval=approval,
    )
    print("Granting the dedicated app's Work IQ delegated admin consent (tenant administrator required).")
    with (
        AzureCliCredential(tenant_id=config.tenant_id) as credential,
        httpx.Client(timeout=60, follow_redirects=False) as client,
        AIProjectClient(endpoint=config.project_endpoint, credential=credential) as project,
    ):
        grant_delegated_consent(config.client_id, credential, client)
        redirect = ensure_connection(config, credential, client)
        configure_redirect(config, redirect, credential, client, ROOT / "terraform/toolbox.auto.tfvars")
        version = ensure_toolbox(config, project)
    print(
        f"Toolbox ready: {config.project_endpoint}/toolboxes/{config.toolbox_name}"
        f"/versions/{version}/mcp?api-version=v1"
    )
    print("Each user still needs to authorize the Foundry OAuth consent link before using Work IQ.")
    print("PDF-byte retrieval and exact page verification remain deferred; do not claim verified pages.")


if __name__ == "__main__":
    main()
