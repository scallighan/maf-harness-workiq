from copy import deepcopy
from unittest.mock import MagicMock

import pytest
from agent_framework import (
    AgentResponse,
    AgentResponseUpdate,
    Content,
    FunctionInvocationContext,
    FunctionTool,
    ResponseStream,
)

from workplace_agent import execution
from workplace_agent.execution import TIMING_KEY, ToolTimingOutput, ToolTimingRecorder, unwrap_tool_output


async def test_runtime_timing_survives_batched_stream_delivery_without_changing_model_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = MagicMock(side_effect=[1_000_000_000, 36_020_000_000])
    monkeypatch.setattr(execution.time, "perf_counter_ns", clock)
    recorder = ToolTimingRecorder()
    function = FunctionTool(name="workiq___ask", func=lambda: "unused")
    result = [Content.from_text('{"answer":"Synthetic result"}')]
    context = FunctionInvocationContext(function=function, arguments={}, metadata={"call_id": "call1"})

    async def execute() -> None:
        context.result = result

    await recorder.process(context, execute)
    source = AgentResponseUpdate(
        role="tool", message_id="tool1",
        contents=[Content.from_function_result("call1", result=result)],
    )
    original_result = deepcopy(source.contents[0].result)
    baseline = AgentResponse.from_updates([deepcopy(source)])

    async def updates():
        yield source

    stream = ResponseStream(updates(), finalizer=AgentResponse.from_updates)
    stream.with_transform_hook(ToolTimingOutput(recorder).annotate)
    outgoing = [update async for update in stream]
    assert ToolTimingOutput(recorder).annotate(outgoing[0]) is outgoing[0]
    assert source.contents[0].result == original_result
    final = await stream.get_final_response()
    timing = outgoing[0].contents[0].result[TIMING_KEY]
    assert timing["duration_ms"] == 35_020
    assert not timing["failed"]
    assert context.result is result
    assert final.messages[0].contents[0].result == baseline.messages[0].contents[0].result
    assert outgoing[0] is not source
    original, parsed = unwrap_tool_output(
        outgoing[0].contents[0].result, call_id="call1", name="workiq___ask",
    )
    assert original == original_result
    assert parsed and parsed.duration_ms == 35_020


async def test_runtime_timing_records_failures_without_swallowing_the_tool_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(execution.time, "perf_counter_ns", MagicMock(side_effect=[0, 2_000_000]))
    recorder = ToolTimingRecorder()
    context = FunctionInvocationContext(
        function=FunctionTool(name="load_skill", func=lambda: "unused"),
        arguments={}, metadata={"call_id": "failed1"},
    )

    async def fail() -> None:
        raise RuntimeError("Synthetic tool failure")

    with pytest.raises(RuntimeError, match="Synthetic tool failure"):
        await recorder.process(context, fail)
    assert recorder.timings["failed1"].failed
    assert recorder.timings["failed1"].duration_ms == 2


@pytest.mark.parametrize("output", [
    [{"type": "text", "text": "Error: Skill 'missing' not found."}],
    [{"type": "text", "text": '{"application/vnd.ms-workiq.retrieval":{"stoppedReason":"error"}}'}],
])
def test_structured_skill_or_retrieval_failures_remain_failures(output: list) -> None:
    assert execution.tool_output_failed(output, skill=True)
