import asyncio
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from agent_framework import AgentContext, AgentResponse, AgentResponseUpdate, Content, ResponseStream
from agent_framework_foundry_hosting._responses import _OutputItemTracker
from azure.ai.agentserver.responses.aio import ResponseEventStream

from workplace_agent.execution import ToolTimingRecorder
from workplace_agent.progress import SOURCE_SEARCH_PREFIX, SourceProgressOutput, SourceSearchProgress
from workplace_agent.ui.events import ResponseEvents
from workplace_agent.ui.models import RunRecord
from workplace_agent.ui.store import now


async def test_source_completion_streams_through_host_before_its_sibling_without_changing_final_response():
    recorder = ToolTimingRecorder()
    progress = SourceSearchProgress(recorder)
    release_files = asyncio.Event()

    async def updates():
        yield AgentResponseUpdate(role="assistant", contents=[
            Content.from_function_call("parent", "workiq___retrieve", arguments={}),
        ])
        token = recorder.active_call_id.set("parent")
        try:
            files = progress.start("OneDriveAndSharePoint")
            email = progress.start("Email")
            progress.finish(email, "completed")
            await release_files.wait()
            progress.finish(files, "completed")
        finally:
            recorder.active_call_id.reset(token)
        yield AgentResponseUpdate(role="tool", contents=[
            Content.from_function_result("parent", result="Original source evidence"),
        ])
        yield AgentResponseUpdate(role="assistant", contents=[Content.from_text("Public answer")])

    original = ResponseStream(updates(), finalizer=AgentResponse.from_updates)
    context = AgentContext(agent=MagicMock(), messages=[], stream=True)

    async def next_handler():
        context.result = original

    await SourceProgressOutput(progress).process(context, next_handler)
    assert isinstance(context.result, ResponseStream)
    record = RunRecord(id=str(uuid4()), session_id=str(uuid4()), prompt_preview="Synthetic", started_at=now())
    adapter = ResponseEvents(record, "https://example.services.ai.azure.com/api/projects/test")
    native = ResponseEventStream(response_id="resp_test")
    adapter.consume(dict(native.emit_created()))
    native.emit_in_progress()
    tracker = _OutputItemTracker(native, frozenset())

    async def consume(update):
        async for event in tracker.handle_update(update):
            adapter.consume(dict(event))

    try:
        async with asyncio.timeout(2):
            while not any(call.source == "Email" and call.status == "completed" for call in record.activity):
                await consume(await anext(context.result))
        assert any(
            call.source == "OneDriveAndSharePoint" and call.status == "running" for call in record.activity
        )
        assert record.activity[0].status == "running"
        assert not release_files.is_set()
    finally:
        release_files.set()
    async for update in context.result:
        await consume(update)
    for event in tracker.close():
        adapter.consume(dict(event))
    assert len(record.activity) == 3
    assert all(call.status == "completed" for call in record.activity)
    assert all(
        call.kind == "tool" and call.parent_call_id == "parent" and call.timing_source == "runtime"
        for call in record.activity if call.source
    )
    final = await context.result.get_final_response()
    assert final is await original.get_final_response()
    assert SOURCE_SEARCH_PREFIX not in str(final.to_dict())
    assert [content.result for message in final.messages for content in message.contents
            if content.type == "function_result"] == ["Original source evidence"]


@pytest.mark.parametrize("fail", [False, True])
async def test_progress_stream_closes_its_sdk_producer_on_cancellation_or_failure(fail: bool):
    progress = SourceSearchProgress(ToolTimingRecorder())
    closed = asyncio.Event()

    async def updates():
        try:
            yield AgentResponseUpdate(role="assistant", contents=[Content.from_text("Started")])
            if fail:
                raise RuntimeError("Synthetic stream failure")
            await asyncio.Event().wait()
        finally:
            closed.set()

    original = ResponseStream(updates(), finalizer=AgentResponse.from_updates)
    context = AgentContext(agent=MagicMock(), messages=[], stream=True)

    async def next_handler():
        context.result = original

    await SourceProgressOutput(progress).process(context, next_handler)
    assert isinstance(context.result, ResponseStream)
    await anext(context.result)
    if fail:
        with pytest.raises(RuntimeError, match="Synthetic stream failure"):
            await anext(context.result)
    else:
        await context.result.close()
    assert closed.is_set()
