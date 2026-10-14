from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from agent_framework import InMemoryHistoryProvider
from agent_framework.exceptions import ToolExecutionException
from agent_framework_foundry_hosting import FoundryToolbox

from workplace_agent import app
from workplace_agent.history import ScopedCosmosHistoryProvider


@pytest.fixture
def factory(monkeypatch: pytest.MonkeyPatch):
    for name, value in {
        "FOUNDRY_PROJECT_ENDPOINT": "https://example.services.ai.azure.com/api/projects/test",
        "AZURE_AI_PROJECT_ENDPOINT": "https://example.services.ai.azure.com/api/projects/test",
        "TOOLBOX_ENDPOINT": "https://example.services.ai.azure.com/api/projects/test/toolboxes/work-iq/mcp",
        "AZURE_AI_MODEL_DEPLOYMENT_NAME": "gpt-5.6-luna",
        "AZURE_TENANT_ID": "tenant",
        "FOUNDRY_AGENT_INSTANCE_CLIENT_ID": "instance",
        "COSMOS_ENDPOINT": "https://example.documents.azure.com",
        "COSMOS_DATABASE": "workplace",
        "COSMOS_HISTORY_CONTAINER": "chatHistory",
    }.items():
        monkeypatch.setenv(name, value)
    config = SimpleNamespace(is_hosted=True, session_id="platform-session")
    context = SimpleNamespace(
        call_id="request-1", user_id="user-1", session_id="platform-session",
        platform_headers=lambda: {"x-agent-foundry-call-id": "request-1"},
    )
    monkeypatch.setattr(app.AgentConfig, "from_env", lambda: config)
    monkeypatch.setattr(app, "get_request_context", lambda: context)
    credentials, clients, transports = [], [], []

    def credential_factory(**kwargs):
        credential = MagicMock(close=AsyncMock())
        credentials.append(credential)
        return credential

    def cosmos_factory(*args, **kwargs):
        client = MagicMock(close=AsyncMock())
        clients.append(client)
        return client

    def transport_factory(*args, **kwargs):
        transport = FoundryToolbox(*args, **kwargs)
        transport.connect = AsyncMock()
        transport.close = AsyncMock()
        transports.append(transport)
        return transport

    monkeypatch.setattr(app, "ManagedIdentityCredential", credential_factory)
    monkeypatch.setattr(app, "AzureCliCredential", credential_factory)
    monkeypatch.setattr(app, "CosmosClient", cosmos_factory)
    monkeypatch.setattr(app, "FoundryToolbox", transport_factory)
    return SimpleNamespace(
        config=config, context=context, credentials=credentials, clients=clients, transports=transports,
    )


async def test_factory_owns_toolbox_and_resources_per_request(factory) -> None:
    first = await app.create_agent()
    second = await app.create_agent()
    assert first is not second
    assert factory.transports[0] is not factory.transports[1]
    async with first:
        factory.transports[0].connect.assert_awaited_once()
        factory.transports[1].connect.assert_not_awaited()
    factory.transports[0].close.assert_awaited_once()
    factory.credentials[0].close.assert_awaited_once()
    factory.clients[0].close.assert_awaited_once()
    async with second:
        factory.transports[1].connect.assert_awaited_once()
    factory.transports[1].close.assert_awaited_once()
    factory.credentials[1].close.assert_awaited_once()


async def test_failed_toolbox_connection_closes_model_history_and_identity(factory) -> None:
    agent = await app.create_agent()
    factory.transports[0].connect.side_effect = RuntimeError("Consent required")
    with pytest.raises(ToolExecutionException, match="Consent required"):
        async with agent:
            pytest.fail("Must not invoke an agent without the user's Toolbox connection.")
    factory.credentials[0].close.assert_awaited_once()
    factory.clients[0].close.assert_awaited_once()


@pytest.mark.parametrize("invalid", ["user", "call", "session", "mismatch"])
async def test_hosted_factory_rejects_untrusted_context(factory, invalid: str) -> None:
    if invalid == "user":
        factory.context.user_id = None
    elif invalid == "call":
        factory.context.call_id = None
    elif invalid == "session":
        factory.config.session_id = ""
    else:
        factory.context.session_id = "another-session"
    with pytest.raises(RuntimeError):
        await app.create_agent()
    assert factory.credentials == []


async def test_factory_selects_loaded_cosmos_history_and_local_memory(
    factory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    build = MagicMock()
    monkeypatch.setattr(app, "create_harness_agent", build)
    await app.create_agent()
    history = build.call_args.kwargs["history_provider"]
    assert isinstance(history, ScopedCosmosHistoryProvider)
    assert history.load_messages
    await build.call_args.kwargs["client"].__aexit__(None, None, None)
    factory.config.is_hosted = False
    factory.context.user_id = None
    await app.create_agent()
    history = build.call_args.kwargs["history_provider"]
    assert isinstance(history, InMemoryHistoryProvider)
    assert history.load_messages
    assert len(factory.clients) == 1
    await build.call_args.kwargs["client"].__aexit__(None, None, None)


def test_responses_server_is_not_an_anonymous_local_workiq_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app.AgentConfig, "from_env", lambda: SimpleNamespace(is_hosted=False))
    with pytest.raises(RuntimeError, match="workplace-demo"):
        app.main()
