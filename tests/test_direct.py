import json
import logging
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from agent_framework import Content, Message
from mcp.types import CallToolResult, TextContent

from workplace_agent import demo, mcp_client
from workplace_agent.auth import (
    WORKIQ_ENDPOINT,
    BrokerWorkIQAuth,
    LocalWorkIQAuth,
    WorkIQAuth,
    WorkIQAuthorizationRequired,
)
from workplace_agent.history import ScopedCosmosHistoryProvider


async def test_direct_mcp_calls_unprefixed_read_tools_and_closes(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="workplace_agent")
    session = MagicMock(initialize=AsyncMock(), call_tool=AsyncMock(return_value=CallToolResult(
        content=[], structuredContent={"base64Content": "abc"},
    )))
    events = []

    @asynccontextmanager
    async def transport(url, **kwargs):
        assert url == WORKIQ_ENDPOINT
        assert kwargs["httpx_client_factory"] is mcp_client.direct_http_client
        events.append("connect")
        yield object(), object(), lambda: "session"
        events.append("disconnect")

    @asynccontextmanager
    async def connect(*args):
        yield session
        events.append("session closed")

    monkeypatch.setattr(mcp_client, "streamablehttp_client", transport)
    monkeypatch.setattr(mcp_client, "ClientSession", connect)
    client = mcp_client.DirectWorkIQClient(httpx.Auth())
    async with client:
        result = await client.call_tool("fetch_blob", path="/me/drive/items/1/content")
        assert json.loads(result)["structuredContent"]["base64Content"] == "abc"
        with pytest.raises(ValueError, match="read-only"):
            await client.call_tool("delete")
    session.call_tool.assert_awaited_once_with("fetch_blob", {"path": "/me/drive/items/1/content"})
    assert WORKIQ_ENDPOINT in caplog.text
    assert "Calling Work IQ tool fetch_blob" in caplog.text
    assert "Work IQ tool fetch_blob returned (is_error=False)" in caplog.text
    assert "/me/drive/items/1/content" not in caplog.text
    assert "base64Content" not in caplog.text
    assert events == ["connect", "session closed", "disconnect"]
    with pytest.raises(RuntimeError, match="not connected"):
        await client.call_tool("ask")


async def test_direct_http_never_follows_redirects() -> None:
    async with mcp_client.direct_http_client() as client:
        assert not client.follow_redirects


@pytest.mark.parametrize("enabled,is_error", [(False, True), (True, False), (True, True)])
async def test_remote_tool_error_details_are_opt_in_and_redact_credentials(
    caplog: pytest.LogCaptureFixture, enabled: bool, is_error: bool,
) -> None:
    auth = WorkIQAuth()
    auth._access_token = "active-secret-token"
    text = (
        'Invalid filter. request_id=request-123; active-secret-token; Bearer bearer-secret; '
        '{"refresh_token":"refresh-secret","client_secret":"client-secret","contentBytes":"binary-secret"} '
        'eyJheader.eyJpayload.signature https://example.com/error?sig=url-secret\n\x1b[31m'
    )
    session = MagicMock(call_tool=AsyncMock(return_value=CallToolResult(
        isError=is_error, content=[TextContent(type="text", text=text)],
        structuredContent={"document": "private-document"},
    )))
    client = mcp_client.DirectWorkIQClient(auth, show_tool_errors=enabled)
    client._session = session
    result = await client.call_tool("search_paths", filter="messages")
    assert json.loads(result)["isError"] is is_error
    if enabled and is_error:
        assert "Invalid filter" in caplog.text
        assert "request-123" in caplog.text
        assert "[redacted]" in caplog.text
        for secret in [
            "active-secret-token", "bearer-secret", "refresh-secret", "client-secret", "binary-secret",
            "eyJheader.eyJpayload.signature", "url-secret", "private-document", "\x1b",
        ]:
            assert secret not in caplog.text
    else:
        assert "Invalid filter" not in caplog.text


async def test_broker_auth_uses_instance_identity_and_caches_only_this_user() -> None:
    credential = MagicMock(get_token=AsyncMock(return_value=SimpleNamespace(token="instance-token")))
    seen = []

    def respond(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, json={"access_token": "user-token", "expires_at": time.time() + 3600})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        auth = BrokerWorkIQAuth(
            "https://broker.example.com", "api://broker", "opaque-user", credential, client,
        )
        for _ in range(2):
            flow = auth.async_auth_flow(httpx.Request("POST", WORKIQ_ENDPOINT))
            request = await anext(flow)
            assert request.headers["Authorization"] == "Bearer user-token"
            await flow.aclose()
        assert len(seen) == 1
        assert seen[0].headers["Authorization"] == "Bearer instance-token"
        assert json.loads(seen[0].content) == {"user_id": "opaque-user"}
        credential.get_token.assert_awaited_once_with("api://broker/.default")
        with pytest.raises(ValueError, match="official"):
            await anext(auth.async_auth_flow(httpx.Request("POST", "https://attacker.example.com")))


async def test_broker_auth_reports_missing_consent_without_identity_fallback() -> None:
    credential = MagicMock(get_token=AsyncMock(return_value=SimpleNamespace(token="instance")))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(409)),
    ) as client:
        auth = BrokerWorkIQAuth("https://broker.example.com", "api://broker", "user", credential, client)
        with pytest.raises(WorkIQAuthorizationRequired, match="https://broker.example.com/login"):
            await auth.acquire()


async def test_broker_auth_retries_a_concurrent_user_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = iter([
        httpx.Response(423),
        httpx.Response(200, json={"access_token": "refreshed", "expires_at": time.time() + 3600}),
    ])
    sleep = AsyncMock()
    monkeypatch.setattr("workplace_agent.auth.asyncio.sleep", sleep)
    credential = MagicMock(get_token=AsyncMock(return_value=SimpleNamespace(token="instance")))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: next(responses)),
    ) as client:
        auth = BrokerWorkIQAuth("https://broker.example.com", "api://broker", "user", credential, client)
        assert (await auth.acquire())[0] == "refreshed"
    sleep.assert_awaited_once_with(2)


async def test_local_auth_keeps_exact_account_and_refreshes_in_memory(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="workplace_agent")
    application = MagicMock()
    account = {"local_account_id": "alice"}
    application.acquire_token_interactive.return_value = {
        "access_token": "alice-token", "expires_in": 3600,
        "id_token_claims": {"tid": "tenant", "oid": "alice"},
    }
    application.get_accounts.return_value = [account, {"local_account_id": "bob"}]
    application.acquire_token_silent.return_value = {"access_token": "alice-refreshed", "expires_in": 3600}
    builder = MagicMock(return_value=application)
    monkeypatch.setattr("workplace_agent.auth.msal.PublicClientApplication", builder)
    auth = LocalWorkIQAuth("tenant", "public-client")
    assert (await auth.acquire())[0] == "alice-token"
    assert (await auth.acquire())[0] == "alice-refreshed"
    assert "https://login.microsoftonline.com" in caplog.text
    assert "Opening Work IQ browser sign-in" in caplog.text
    assert "Work IQ authorization ready (cached account)" in caplog.text
    assert "alice-token" not in caplog.text
    assert "alice-refreshed" not in caplog.text
    assert application.acquire_token_silent.call_args.kwargs["account"] == account
    assert builder.call_args.kwargs["token_cache"].serialize() == "{}"
    application.acquire_token_silent.return_value = None
    application.acquire_token_interactive.return_value = {
        "access_token": "bob-token", "expires_in": 3600,
        "id_token_claims": {"tid": "tenant", "oid": "bob"},
    }
    with pytest.raises(WorkIQAuthorizationRequired, match="Restart the demo"):
        await auth.acquire()
    application.acquire_token_interactive.return_value = {"error": "interaction_required"}
    with pytest.raises(WorkIQAuthorizationRequired):
        await auth.acquire()


async def test_scoped_history_loads_saved_partition_and_isolates_callers() -> None:
    provider = MagicMock(get_messages=AsyncMock(return_value=[Message("user", "previous")]),
                         save_messages=AsyncMock())

    def scope(**changes):
        options = dict(tenant_id="tenant", project_endpoint="project", user_id="alice",
                       platform_session_id="platform")
        options.update(changes)
        return ScopedCosmosHistoryProvider(provider, **options)

    history = scope()
    key = history.partition("conversation")
    assert "alice" not in key
    assert key != scope(user_id="bob").partition("conversation")
    assert key != scope(tenant_id="another").partition("conversation")
    assert key != scope(platform_session_id="another").partition("conversation")
    assert key != history.partition("another-conversation")
    messages = await history.get_messages("conversation")
    await history.save_messages("conversation", messages)
    provider.get_messages.assert_awaited_once_with(key)
    provider.save_messages.assert_awaited_once_with(key, messages)
    with pytest.raises(ValueError, match="session ID"):
        await history.get_messages(None)


async def test_demo_reuses_memory_session_and_resets_only_on_new(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = MagicMock()
    agent.__aenter__ = AsyncMock(return_value=agent)
    agent.__aexit__ = AsyncMock()
    agent.create_session.side_effect = ["first-session", "second-session"]
    calls = []

    async def run(prompt, **kwargs):
        calls.append((prompt, kwargs["session"]))
        yield SimpleNamespace(text="answer", contents=[])

    agent.run = run
    monkeypatch.setattr(demo, "create_agent", AsyncMock(return_value=agent))
    monkeypatch.setattr(demo.AgentConfig, "from_env", lambda: SimpleNamespace(is_hosted=False))
    inputs = iter(["first", "second", "/new", "third", "/quit"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    await demo.run_demo()
    assert calls == [("first", "first-session"), ("second", "first-session"), ("third", "second-session")]
    agent.__aexit__.assert_awaited_once()


@pytest.mark.parametrize("answer,approved", [("yes", True), ("", False)])
async def test_demo_resolves_tool_approvals_before_accepting_another_turn(
    monkeypatch: pytest.MonkeyPatch, answer: str, approved: bool,
) -> None:
    function_call = Content.from_function_call(
        call_id="skill-call", name="load_skill", arguments='{"skill_name":"document-finder"}',
    )
    approval = Content.from_function_approval_request(id="skill-approval", function_call=function_call)
    agent = MagicMock()
    agent.__aenter__ = AsyncMock(return_value=agent)
    agent.__aexit__ = AsyncMock()
    agent.create_session.return_value = "same-session"
    calls = []

    async def run(messages, **kwargs):
        calls.append((messages, kwargs["session"]))
        if messages == "first":
            yield SimpleNamespace(text="", contents=[approval, approval])
        else:
            yield SimpleNamespace(text="answer", contents=[])

    agent.run = run
    monkeypatch.setattr(demo, "create_agent", AsyncMock(return_value=agent))
    monkeypatch.setattr(demo.AgentConfig, "from_env", lambda: SimpleNamespace(is_hosted=False))
    inputs = iter(["first", "invalid", answer, "follow-up", "/quit"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    await demo.run_demo()
    assert len(calls) == 3
    responses = calls[1][0]
    assert len(responses) == 1
    assert responses[0].type == "function_approval_response"
    assert responses[0].id == approval.id
    assert responses[0].function_call is function_call
    assert responses[0].approved is approved
    assert calls[2][0] == "follow-up"
    assert all(session == "same-session" for _, session in calls)
    agent.__aexit__.assert_awaited_once()


async def test_demo_exits_cleanly_on_eof_during_tool_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    approval = Content.from_function_approval_request(
        id="skill-approval",
        function_call=Content.from_function_call(
            call_id="skill-call", name="load_skill", arguments='{"skill_name":"document-finder"}',
        ),
    )
    agent = MagicMock()
    agent.__aenter__ = AsyncMock(return_value=agent)
    agent.__aexit__ = AsyncMock()
    calls = []

    async def run(messages, **kwargs):
        calls.append(messages)
        yield SimpleNamespace(text="", contents=[approval])

    inputs = iter(["first"])

    def read_input(_):
        try:
            return next(inputs)
        except StopIteration:
            raise EOFError from None

    agent.run = run
    monkeypatch.setattr(demo, "create_agent", AsyncMock(return_value=agent))
    monkeypatch.setattr(demo.AgentConfig, "from_env", lambda: SimpleNamespace(is_hosted=False))
    monkeypatch.setattr("builtins.input", read_input)
    await demo.run_demo()
    assert calls == ["first"]
    agent.__aexit__.assert_awaited_once()


@pytest.mark.parametrize("verbose", [False, True])
def test_demo_verbose_enables_only_application_logging(
    monkeypatch: pytest.MonkeyPatch, verbose: bool,
) -> None:
    application_logger = logging.getLogger("workplace_agent")
    monkeypatch.setattr(application_logger, "level", logging.WARNING)
    configure_logging = MagicMock()
    run = MagicMock()
    build = MagicMock()
    monkeypatch.setattr(demo.logging, "basicConfig", configure_logging)
    monkeypatch.setattr(demo.asyncio, "run", run)
    monkeypatch.setattr(demo, "run_demo", build)
    monkeypatch.setattr("sys.argv", ["workplace-demo", *(["--verbose"] if verbose else [])])
    demo.main()
    configure_logging.assert_called_once_with(level=logging.WARNING)
    assert application_logger.level == (logging.INFO if verbose else logging.WARNING)
    build.assert_called_once_with()
    run.assert_called_once_with(build.return_value)

