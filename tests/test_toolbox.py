import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from agent_framework.exceptions import ToolExecutionException
from azure.ai.projects.models import WorkIQPreviewToolboxTool
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData

from scripts import setup_toolbox
from workplace_agent import demo

CONFIG = setup_toolbox.SetupConfig(
    project_endpoint="https://example.services.ai.azure.com/api/projects/test",
    project_id="/subscriptions/sub/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/a/projects/test",
    tenant_id="tenant",
    client_id="client",
    application_object_id="application",
    toolbox_name="work-iq-toolbox",
)
REDIRECT = "https://example.services.ai.azure.com/oauth/callback"


def connection_properties() -> dict:
    return {
        "target": setup_toolbox.WORKIQ_A2A_ENDPOINT,
        "authType": "OAuth2",
        "metadata": {"WorkIQClientId": CONFIG.client_id},
        "redirectUrl": REDIRECT,
    }


def credential() -> MagicMock:
    return MagicMock(get_token=MagicMock(return_value=MagicMock(token="azure-token")))


def test_setup_creates_documented_connection_without_persisting_or_printing_secret(
    tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(404)
        if request.url.path.endswith("/addPassword"):
            return httpx.Response(200, json={"secretText": "private-client-secret", "keyId": "key"})
        if request.method == "PUT":
            return httpx.Response(201, json={"properties": connection_properties()})
        return httpx.Response(204)

    path = tmp_path / "toolbox.auto.tfvars"
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        identity = credential()
        redirect = setup_toolbox.ensure_connection(CONFIG, identity, client)
        setup_toolbox.configure_redirect(CONFIG, redirect, identity, client, path)
    put = next(request for request in calls if request.method == "PUT")
    properties = json.loads(put.content)["properties"]
    assert properties["category"] == "RemoteA2A"
    assert properties["target"] == setup_toolbox.WORKIQ_A2A_ENDPOINT
    assert properties["Scopes"] == [setup_toolbox.WORKIQ_SCOPE, "offline_access"]
    assert properties["Credentials"]["ClientSecret"] == "private-client-secret"
    patch = next(request for request in calls if request.method == "PATCH")
    assert json.loads(patch.content) == {"web": {"redirectUris": [REDIRECT]}}
    assert path.read_text() == f"workiq_toolbox_redirect_uri = {json.dumps(REDIRECT)}\n"
    assert list(tmp_path.iterdir()) == [path]
    assert "private-client-secret" not in capsys.readouterr().out


def test_setup_reuses_connection_and_toolbox_without_rotating_credentials() -> None:
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
        tools=[WorkIQPreviewToolboxTool(project_connection_id=CONFIG.connection_id)],
    )
    assert setup_toolbox.ensure_toolbox(CONFIG, project) == "2"
    project.toolboxes.create_version.assert_not_called()
    project.toolboxes.update.assert_not_called()


def test_setup_removes_new_password_when_connection_creation_fails() -> None:
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(404)
        if request.url.path.endswith("/addPassword"):
            return httpx.Response(200, json={"secretText": "secret", "keyId": "new-key"})
        if request.method == "PUT":
            return httpx.Response(400)
        return httpx.Response(204)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            setup_toolbox.ensure_connection(CONFIG, credential(), client)
    assert requests[-1].url.path.endswith("/removePassword")
    assert json.loads(requests[-1].content) == {"keyId": "new-key"}


def test_setup_refuses_to_overwrite_another_apps_connection() -> None:
    properties = connection_properties()
    properties["metadata"] = {"WorkIQClientId": "another-client"}
    with httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"properties": properties}),
    )) as client:
        with pytest.raises(RuntimeError, match="does not match"):
            setup_toolbox.ensure_connection(CONFIG, credential(), client)


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
