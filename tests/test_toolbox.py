import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from agent_framework.exceptions import ToolExecutionException
from azure.ai.projects.models import MCPToolboxTool, WorkIQPreviewToolboxTool
from azure.core.exceptions import ResourceNotFoundError
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData

from scripts import setup_toolbox
from workplace_agent import demo

CONFIG = setup_toolbox.SetupConfig(
    project_endpoint="https://example.services.ai.azure.com/api/projects/test",
    connection_id="/subscriptions/sub/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/a/projects/test/connections/work-iq-mcp",
    tenant_id="tenant",
    client_id="client",
    application_object_id="application",
    toolbox_name="work-iq-toolbox",
    allowed_tools=("ask", "fetch", "get_schema", "search_paths", "fetch_blob"),
    require_approval="always",
)
REDIRECT = "https://example.services.ai.azure.com/oauth/callback"


def connection_properties() -> dict:
    return {
        "target": setup_toolbox.WORKIQ_MCP_ENDPOINT,
        "category": "RemoteTool",
        "authType": "OAuth2",
        "authorizationUrl": "https://login.microsoftonline.com/tenant/oauth2/v2.0/authorize",
        "tokenUrl": "https://login.microsoftonline.com/tenant/oauth2/v2.0/token",
        "scopes": [setup_toolbox.WORKIQ_SCOPE, "offline_access"],
        "redirectUrl": REDIRECT,
    }


def credential() -> MagicMock:
    return MagicMock(get_token=MagicMock(return_value=MagicMock(token="azure-token")))


def test_setup_reads_terraform_connection_without_issuing_or_persisting_credentials(
    tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "GET" and request.url.host == "graph.microsoft.com":
            return httpx.Response(200, json={"web": {"redirectUris": []}})
        if request.method == "GET":
            return httpx.Response(200, json={"properties": connection_properties()})
        return httpx.Response(204)

    path = tmp_path / "toolbox.auto.tfvars"
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        identity = credential()
        redirect = setup_toolbox.ensure_connection(CONFIG, identity, client)
        setup_toolbox.configure_redirect(CONFIG, redirect, identity, client, path)
    assert [request.method for request in calls] == ["GET", "GET", "PATCH"]
    patch = next(request for request in calls if request.method == "PATCH")
    assert json.loads(patch.content) == {"web": {"redirectUris": [REDIRECT]}}
    assert path.read_text() == f"workiq_toolbox_redirect_uri = {json.dumps(REDIRECT)}\n"
    assert list(tmp_path.iterdir()) == [path]
    assert "azure-token" not in capsys.readouterr().out


@pytest.mark.parametrize("existing", [
    ["https://global.consent.azure-apim.net/redirect/consent"],
    ["https://global.consent.azure-apim.net/redirect/consent", REDIRECT],
])
def test_setup_preserves_foundry_consent_redirects(tmp_path: Path, existing: list[str]) -> None:
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"web": {"redirectUris": existing}})
        return httpx.Response(204)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        setup_toolbox.configure_redirect(
            CONFIG, REDIRECT, credential(), client, tmp_path / "toolbox.auto.tfvars",
        )
    if REDIRECT in existing:
        assert [request.method for request in requests] == ["GET"]
    else:
        assert json.loads(requests[-1].content) == {"web": {"redirectUris": [*existing, REDIRECT]}}


def mcp_tool() -> MCPToolboxTool:
    return MCPToolboxTool(
        server_label="workiq", server_url=setup_toolbox.WORKIQ_MCP_ENDPOINT,
        project_connection_id=CONFIG.connection_id,
        allowed_tools=list(CONFIG.allowed_tools), require_approval=CONFIG.require_approval,
    )


def test_setup_reuses_connection_and_matching_mcp_toolbox() -> None:
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"properties": connection_properties()})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        assert setup_toolbox.ensure_connection(CONFIG, credential(), client) == REDIRECT
    assert len(requests) == 1
    assert requests[0].method == "GET"
    project = MagicMock()
    project.toolboxes.get.return_value = MagicMock(default_version="2")
    project.toolboxes.get_version.return_value = MagicMock(
        name=CONFIG.toolbox_name, version="2",
        tools=[mcp_tool()],
    )
    assert setup_toolbox.ensure_toolbox(CONFIG, project) == "2"
    project.toolboxes.create_version.assert_not_called()
    project.toolboxes.update.assert_not_called()


def test_setup_missing_connection_requires_terraform_instead_of_creating_credentials() -> None:
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            setup_toolbox.ensure_connection(CONFIG, credential(), client)
    assert len(requests) == 1
    assert requests[0].method == "GET"


@pytest.mark.parametrize("changed", [
    {"category": "RemoteA2A"},
    {"target": "https://other.example.com/mcp"},
    {"authorizationUrl": "https://login.microsoftonline.com/other/oauth2/v2.0/authorize"},
    {"scopes": ["unexpected.scope"]},
])
def test_setup_refuses_mismatched_connection(changed: dict) -> None:
    properties = connection_properties()
    properties.update(changed)
    with httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"properties": properties}),
    )) as client:
        with pytest.raises(RuntimeError, match="does not match"):
            setup_toolbox.ensure_connection(CONFIG, credential(), client)


@pytest.mark.parametrize("previous", [
    WorkIQPreviewToolboxTool(project_connection_id=CONFIG.connection_id),
    MCPToolboxTool(
        server_label="workiq", server_url=setup_toolbox.WORKIQ_MCP_ENDPOINT,
        project_connection_id=CONFIG.connection_id, allowed_tools=["ask"], require_approval="never",
    ),
])
def test_setup_publishes_generic_mcp_version_with_requested_controls(previous) -> None:
    project = MagicMock()
    project.toolboxes.get.return_value = MagicMock(default_version="1")
    project.toolboxes.get_version.return_value = MagicMock(tools=[previous])
    project.toolboxes.create_version.return_value = MagicMock(name=CONFIG.toolbox_name, version="2")
    assert setup_toolbox.ensure_toolbox(CONFIG, project) == "2"
    published = project.toolboxes.create_version.call_args.kwargs["tools"]
    assert len(published) == 1
    assert published[0].as_dict() == mcp_tool().as_dict()
    project.toolboxes.update.assert_called_once_with(CONFIG.toolbox_name, default_version="2")


def test_setup_first_toolbox_uses_automatic_default_version() -> None:
    project = MagicMock()
    project.toolboxes.get.side_effect = ResourceNotFoundError("not found")
    project.toolboxes.create_version.return_value = MagicMock(name=CONFIG.toolbox_name, version="1")
    assert setup_toolbox.ensure_toolbox(CONFIG, project) == "1"
    project.toolboxes.update.assert_not_called()


def test_setup_failed_publication_does_not_promote_a_version() -> None:
    project = MagicMock()
    project.toolboxes.get_version.return_value = MagicMock(tools=[])
    project.toolboxes.create_version.side_effect = RuntimeError("publish failed")
    with pytest.raises(RuntimeError, match="publish failed"):
        setup_toolbox.ensure_toolbox(CONFIG, project)
    project.toolboxes.update.assert_not_called()


def consent_error(link: str) -> ToolExecutionException:
    details = {"errors": [{
        "name": "work-iq", "type": "a2a_preview",
        "error": {"code": "CONSENT_REQUIRED", "message": link},
    }]}
    return ToolExecutionException("Toolbox consent required", inner_exception=McpError(ErrorData(
        code=-32006, message="tools/list failed " + json.dumps(details),
    )))


async def test_local_demo_displays_consent_and_stops_instead_of_using_a_fallback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    agent = MagicMock()
    agent.__aenter__ = AsyncMock(side_effect=consent_error("https://example.com/consent"))
    agent.__aexit__ = AsyncMock()
    monkeypatch.setattr(demo.AgentConfig, "from_env", lambda: MagicMock(is_hosted=False))
    monkeypatch.setattr(demo, "create_agent", AsyncMock(return_value=agent))
    with pytest.raises(SystemExit) as raised:
        await demo.run_demo()
    assert raised.value.code == 1
    output = capsys.readouterr().out
    assert "https://example.com/consent" in output
    assert "rerun the local demo" in output
    agent.run.assert_not_called()


@pytest.mark.parametrize("link", ["http://example.com/consent", "https://user:secret@example.com/consent"])
def test_local_demo_rejects_unsafe_consent_links(link: str) -> None:
    with pytest.raises(RuntimeError, match="invalid OAuth consent link"):
        demo.consent_links(consent_error(link))
