import asyncio
import json
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from agent_framework import AgentSession, Content, FunctionInvocationContext, FunctionTool
from agent_framework.exceptions import ToolExecutionException

from workplace_agent.ask import ASK_STATE_KEY, DEFAULT_AGENT_ID, AskSessionContext


def ask_context(session: AgentSession, **arguments) -> FunctionInvocationContext:
    return FunctionInvocationContext(
        function=FunctionTool(name="workiq___ask", func=lambda: "unused"),
        arguments={"question": "Synthetic question", **arguments}, session=session,
    )


def ask_result(identifier: str) -> list[Content]:
    return [Content.from_text(json.dumps({
        "response": "Synthetic answer", "conversationId": identifier,
    }), additional_properties={"_meta": {"source": "synthetic"}})]


async def test_ask_continues_after_session_restore_without_trusting_model_ids():
    session = AgentSession()
    context = ask_context(session, conversationId="another-session", fileUrls=["https://example.com/file"])
    result = ask_result("workiq-thread")
    assert isinstance(context.arguments, dict)
    original = deepcopy(context.arguments)

    async def first_call():
        assert context.arguments == {
            **original, "conversationId": None, "agentId": DEFAULT_AGENT_ID,
        }
        context.result = result

    await AskSessionContext().process(context, first_call)
    assert context.result is result
    assert session.state[ASK_STATE_KEY] == {
        "agent_id": DEFAULT_AGENT_ID, "conversation_id": "workiq-thread",
    }
    restored = AgentSession.from_dict(json.loads(json.dumps(session.to_dict())))
    follow_up = ask_context(restored, conversationId="invented-id")

    async def next_call():
        assert isinstance(follow_up.arguments, dict)
        assert follow_up.arguments["conversationId"] == "workiq-thread"
        follow_up.result = result

    await AskSessionContext().process(follow_up, next_call)
    unrelated = ask_context(AgentSession(), conversationId="workiq-thread")

    async def separate_call():
        assert isinstance(unrelated.arguments, dict)
        assert unrelated.arguments["conversationId"] is None
        unrelated.result = ask_result("separate-thread")

    await AskSessionContext().process(unrelated, separate_call)
    assert restored.state[ASK_STATE_KEY]["conversation_id"] == "workiq-thread"


async def test_concurrent_first_ask_calls_share_one_remote_conversation():
    middleware = AskSessionContext()
    session = AgentSession()
    first, second = ask_context(session), ask_context(session)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def first_call():
        assert isinstance(first.arguments, dict)
        calls.append(first.arguments["conversationId"])
        entered.set()
        await release.wait()
        first.result = ask_result("one-thread")

    async def second_call():
        assert isinstance(second.arguments, dict)
        calls.append(second.arguments["conversationId"])
        second.result = ask_result("one-thread")

    task = asyncio.create_task(middleware.process(first, first_call))
    await entered.wait()
    other = asyncio.create_task(middleware.process(second, second_call))
    try:
        await asyncio.sleep(0)
        assert calls == [None]
    finally:
        release.set()
        await asyncio.gather(task, other)
    assert calls == [None, "one-thread"]


@pytest.mark.parametrize("result", [
    [Content.from_text("Plain answer without metadata")],
    [Content.from_text('{"response":{"conversationId":"outlook-mail-thread"}}')],
    [Content.from_text('{"conversationId":null}')],
    [Content.from_text('{"conversationId":"first"}'), Content.from_text('{"conversationId":"second"}')],
])
async def test_unusable_success_metadata_cannot_silently_start_another_thread(result):
    session = AgentSession()
    context = ask_context(session)

    async def execute():
        context.result = result

    middleware = AskSessionContext()
    with pytest.raises(ToolExecutionException, match="conversation"):
        await middleware.process(context, execute)
    next_call = AsyncMock()
    with pytest.raises(ToolExecutionException, match="start a new session"):
        await middleware.process(ask_context(session), next_call)
    next_call.assert_not_awaited()


async def test_ask_errors_and_changed_ids_never_reset_established_context():
    session = AgentSession()
    session.state[ASK_STATE_KEY] = {"agent_id": "selected-agent", "conversation_id": "original-thread"}
    context = ask_context(session)
    failure = [Content.from_text('{"isError":true,"error":"Access denied"}')]

    async def denied():
        assert isinstance(context.arguments, dict)
        assert context.arguments["agentId"] == "selected-agent"
        assert context.arguments["conversationId"] == "original-thread"
        context.result = failure

    middleware = AskSessionContext()
    await middleware.process(context, denied)
    assert context.result is failure

    async def changed():
        context.result = ask_result("replacement-thread")

    with pytest.raises(ToolExecutionException, match="different conversation ID"):
        await middleware.process(context, changed)
    assert session.state[ASK_STATE_KEY]["conversation_id"] == "original-thread"
    execute = AsyncMock()
    with pytest.raises(ToolExecutionException, match="cannot change agents"):
        await middleware.process(ask_context(session, agentId="other-agent"), execute)
    execute.assert_not_awaited()

    async def unavailable():
        raise RuntimeError("Synthetic transport failure")

    with pytest.raises(RuntimeError, match="Synthetic transport failure"):
        await middleware.process(context, unavailable)
    assert session.state[ASK_STATE_KEY]["conversation_id"] == "original-thread"


async def test_ask_requires_session_but_does_not_intercept_other_tools():
    middleware = AskSessionContext()
    execute = AsyncMock()
    context = FunctionInvocationContext(
        function=FunctionTool(name="workiq___ask", func=lambda: "unused"), arguments={},
    )
    with pytest.raises(ToolExecutionException, match="active conversation session"):
        await middleware.process(context, execute)
    execute.assert_not_awaited()
    context.function = FunctionTool(name="workiq___retrieve", func=lambda: "unused")
    await middleware.process(context, execute)
    execute.assert_awaited_once()
