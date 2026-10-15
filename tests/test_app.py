import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from agent_framework import (
    AgentSession,
    BaseChatClient,
    ChatMiddlewareLayer,
    ChatResponse,
    ChatResponseUpdate,
    Content,
    FunctionInvocationLayer,
    FunctionTool,
    InMemoryHistoryProvider,
    ResponseStream,
    SessionContext,
    SkillsProvider,
)
from agent_framework.exceptions import ToolExecutionException
from openai.types.responses import Response

from workplace_agent import app
from workplace_agent.execution import TIMING_KEY, unwrap_tool_output
from workplace_agent.history import ScopedCosmosHistoryProvider
from workplace_agent.retrieval import ParallelWorkIQToolbox


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
        transport = ParallelWorkIQToolbox(*args, **kwargs)
        transport.connect = AsyncMock()
        transport.close = AsyncMock()
        transports.append(transport)
        return transport

    monkeypatch.setattr(app, "ManagedIdentityCredential", credential_factory)
    monkeypatch.setattr(app, "AzureCliCredential", credential_factory)
    monkeypatch.setattr(app, "CosmosClient", cosmos_factory)
    monkeypatch.setattr(app, "ParallelWorkIQToolbox", transport_factory)
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


@pytest.mark.parametrize("skill_name", ["document-finder", "meeting-prep"])
async def test_factory_exposes_packaged_skills_with_only_local_read_operations_approved(
    factory, monkeypatch: pytest.MonkeyPatch, skill_name: str,
) -> None:
    build = MagicMock(wraps=app.create_harness_agent)
    monkeypatch.setattr(app, "create_harness_agent", build)
    agent = await app.create_agent()
    try:
        provider = SkillsProvider.from_paths(build.call_args.kwargs["skills_paths"])
        context = SessionContext(input_messages=[])
        await provider.before_run(agent=agent, session=agent.create_session(), context=context, state={})
        load = next(tool for tool in context.tools if tool.name == "load_skill")
        instructions = await load.invoke(arguments={"skill_name": skill_name}, skip_parsing=True)
        if skill_name == "document-finder":
            assert "PowerPoint" in instructions
            assert "Spreadsheet" in instructions
            assert "Audio/video" in instructions
        else:
            assert "Meetings" in instructions
            assert "Email" in instructions and "OneDriveAndSharePoint" in instructions
            assert "Opening talk track" in instructions
        assert "inspect_document" not in instructions
        rules = build.call_args.kwargs["auto_approval_rules"]
        assert any(rule(Content.from_function_call("call1", "load_skill")) for rule in rules)
        assert not any(rule(Content.from_function_call("call2", "run_skill_script")) for rule in rules)
        assert not any(rule(Content.from_function_call(
            "call3", "load_skill", additional_properties={"server_label": "remote"},
        )) for rule in rules)
    finally:
        await build.call_args.kwargs["client"].__aexit__(None, None, None)


async def test_factory_reuses_ask_context_through_real_tool_invocation_across_requests(
    factory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SyntheticClient(FunctionInvocationLayer, ChatMiddlewareLayer, BaseChatClient):
        def __init__(self, **kwargs):
            super().__init__()
            self.project_client = MagicMock(close=AsyncMock())
            self.client = MagicMock(close=AsyncMock())
            self.calls = 0

        def _inner_get_response(self, *, messages, stream, options, **kwargs):
            self.calls += 1
            first = self.calls == 1

            async def updates():
                yield ChatResponseUpdate(
                    role="assistant", finish_reason="tool_calls" if first else "stop",
                    contents=[
                        Content.from_function_call("ask1", "workiq___ask", arguments={
                            "question": "Synthetic question", "conversationId": "untrusted-id",
                        }) if first else Content.from_text("Ready"),
                    ],
                )

            return ResponseStream(updates(), finalizer=ChatResponse.from_updates)

    calls = []

    async def ask(question: str, conversationId: str | None = None, agentId: str | None = None) -> str:
        calls.append(conversationId)
        return json.dumps({"response": "Synthetic answer", "conversationId": "runtime-thread"})

    factory.config.is_hosted = False
    monkeypatch.setattr(app, "FoundryChatClient", SyntheticClient)
    session = AgentSession()
    for _ in range(2):
        agent = await app.create_agent()
        agent.default_options["tools"].append(
            FunctionTool(name="workiq___ask", func=ask, approval_mode="never_require"),
        )
        async with agent:
            async for _ in agent.run("Synthetic Ask continuity check.", session=session, stream=True):
                pass
        session = AgentSession.from_dict(json.loads(json.dumps(session.to_dict())))
    assert calls == [None, "runtime-thread"]


async def test_factory_streams_timing_outside_lazy_approval_without_changing_model_or_history(
    factory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SyntheticClient(FunctionInvocationLayer, ChatMiddlewareLayer, BaseChatClient):
        def __init__(self, **kwargs):
            super().__init__()
            self.project_client = MagicMock(close=AsyncMock())
            self.client = MagicMock(close=AsyncMock())
            self.requests = []
            self.options = []

        def _inner_get_response(self, *, messages, stream, options, **kwargs):
            self.requests.append(list(messages))
            self.options.append(dict(options))
            first = len(self.requests) == 1

            async def updates():
                yield ChatResponseUpdate(
                    role="assistant", finish_reason="tool_calls" if first else "stop",
                    contents=[
                        Content.from_function_call(
                            "skill1", "load_skill", arguments='{"skill_name":"document-finder"}',
                        ) if first else Content.from_text("Ready"),
                    ],
                )

            return ResponseStream(updates(), finalizer=ChatResponse.from_updates)

    factory.config.is_hosted = False
    monkeypatch.setattr(app, "FoundryChatClient", SyntheticClient)
    agent = await app.create_agent()
    session = agent.create_session()
    async with agent:
        stream = agent.run("Load the document-finder skill.", session=session, stream=True)
        results = [
            content for update in [update async for update in stream] for content in update.contents
            if content.type == "function_result"
        ]
        assert len(results) == 1
        original, timing = unwrap_tool_output(results[0].result, call_id="skill1", name="load_skill")
        assert timing and timing.duration_ms >= 0 and not timing.failed
        assert isinstance(original, str) and "OneDriveAndSharePoint" in original
        final = await stream.get_final_response()
        assert all(
            TIMING_KEY not in str(content.result)
            for message in final.messages for content in message.contents if content.type == "function_result"
        )
        assert isinstance(agent.client, SyntheticClient)
        assert all(options["reasoning"] == {"effort": "low"} for options in agent.client.options)
        assert all(
            TIMING_KEY not in str(content.result)
            for request in agent.client.requests for message in request for content in message.contents
            if content.type == "function_result"
        )
        assert TIMING_KEY not in str(session.state)


async def test_factory_sends_low_reasoning_to_foundry_without_changing_model_or_storage(
    factory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory.config.is_hosted = False
    agent = await app.create_agent()
    response = Response.model_validate({
        "id": "resp_test", "object": "response", "created_at": 1,
        "model": "gpt-5.6-luna", "status": "completed",
        "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
        "output": [{
            "id": "msg_test", "type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": "Ready", "annotations": []}],
        }],
    })
    complete = AsyncMock(return_value=MagicMock(parse=MagicMock(return_value=response), headers={}))
    assert isinstance(agent.client, app.FoundryChatClient)
    monkeypatch.setattr(agent.client.client.responses.with_raw_response, "create", complete)
    async with agent:
        result = await agent.run("Reply Ready.", session=agent.create_session())
    assert result.text == "Ready"
    complete.assert_awaited_once()
    request = complete.call_args.kwargs
    assert request["reasoning"] == {"effort": "low"}
    assert request["model"] == "gpt-5.6-luna"
    assert request["store"] is False
    assert any(tool.get("name") == "load_skill" for tool in request["tools"])


def test_responses_server_is_not_an_anonymous_local_workiq_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app.AgentConfig, "from_env", lambda: SimpleNamespace(is_hosted=False))
    with pytest.raises(RuntimeError, match="workplace-demo"):
        app.main()
