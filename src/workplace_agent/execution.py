from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from copy import copy
from datetime import UTC, datetime
from typing import Literal

from agent_framework import (
    AgentContext,
    AgentMiddleware,
    AgentResponseUpdate,
    ChatContext,
    ChatMiddleware,
    Content,
    FunctionInvocationContext,
    FunctionMiddleware,
)
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)
TIMING_KEY = "__workiq_tool_execution__"


class ExecutionTiming(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    call_id: str = Field(min_length=1, max_length=512)
    name: str = Field(min_length=1, max_length=512)
    started_at: AwareDatetime
    duration_ms: int = Field(ge=0, strict=True)
    failed: bool


class ToolTimingRecorder(FunctionMiddleware):
    def __init__(self) -> None:
        self.timings: dict[str, ExecutionTiming] = {}
        self.active_call_id: ContextVar[str | None] = ContextVar("workiq_active_call_id", default=None)

    async def process(
        self, context: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]],
    ) -> None:
        call_id = context.metadata.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            logger.warning("Tool timing unavailable: invocation has no call ID.")
            await call_next()
            return
        started_at = datetime.now(UTC)
        started = time.perf_counter_ns()
        failed = True
        token = self.active_call_id.set(call_id)
        try:
            await call_next()
            failed = tool_output_failed(context.result, skill=context.function.name == "load_skill")
        finally:
            self.active_call_id.reset(token)
            self.timings[call_id] = ExecutionTiming(
                call_id=call_id, name=context.function.name, started_at=started_at,
                duration_ms=(time.perf_counter_ns() - started) // 1_000_000, failed=failed,
            )


class ModelTimingLogger(ChatMiddleware):
    async def process(self, context: ChatContext, call_next: Callable[[], Awaitable[None]]) -> None:
        started = time.perf_counter_ns()
        first_update_ms: int | None = None
        reported = False

        def first_update(update):
            nonlocal first_update_ms
            if first_update_ms is None:
                first_update_ms = (time.perf_counter_ns() - started) // 1_000_000
            return update

        def completed(response):
            nonlocal reported
            reported = True
            logger.info(
                "Model call completed: elapsed_ms=%d first_update_ms=%s",
                (time.perf_counter_ns() - started) // 1_000_000, first_update_ms,
            )
            return response

        def closed() -> None:
            if not reported:
                logger.warning("Model call interrupted before completion.")

        if context.stream:
            context.stream_update_transforms.append(first_update)
            context.stream_result_transforms.append(completed)
            context.stream_cleanup_hooks.append(closed)
        await call_next()
        if not context.stream:
            completed(context.result)


class ToolTimingOutput(AgentMiddleware):
    def __init__(self, recorder: ToolTimingRecorder) -> None:
        self.recorder = recorder

    async def process(self, context: AgentContext, call_next: Callable[[], Awaitable[None]]) -> None:
        if context.stream:
            context.stream_update_transforms.append(self.annotate)
        await call_next()

    def annotate(self, update: AgentResponseUpdate) -> AgentResponseUpdate:
        contents = []
        changed = False
        for content in update.contents:
            timing = self.recorder.timings.get(content.call_id or "")
            if content.type == "function_result" and timing:
                metadata = timing.model_dump(mode="json")
                if (
                    isinstance(content.result, dict)
                    and set(content.result) == {TIMING_KEY, "output"}
                    and content.result[TIMING_KEY] == metadata
                ):
                    contents.append(content)
                    continue
                # Annotate only the outgoing stream; model results and history stay unchanged.
                content = copy(content)
                content.result = {
                    TIMING_KEY: metadata,
                    "output": content.result,
                }
                changed = True
            contents.append(content)
        if not changed:
            return update
        outgoing = copy(update)
        outgoing.contents = contents
        return outgoing


def unwrap_tool_output(
    output: object, *, call_id: str, name: str | None,
) -> tuple[object, ExecutionTiming | None]:
    value = output
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return output, None
    if not isinstance(value, dict) or TIMING_KEY not in value:
        return output, None
    if set(value) != {TIMING_KEY, "output"}:
        raise ValueError("The hosted tool timing envelope is invalid.")
    timing = ExecutionTiming.model_validate(value[TIMING_KEY])
    if timing.call_id != call_id or timing.name != name:
        raise ValueError("The hosted tool timing does not match this call.")
    return value["output"], timing


def tool_output_failed(output: object, *, skill: bool) -> bool:
    if isinstance(output, Content):
        return output.type == "text" and tool_output_failed(output.text, skill=skill)
    if isinstance(output, str):
        try:
            value = json.loads(output)
        except json.JSONDecodeError:
            return skill and output.startswith("Error:")
        return tool_output_failed(value, skill=skill)
    if isinstance(output, list):
        return any(
            tool_output_failed(item, skill=skill) if isinstance(item, Content)
            else tool_output_failed(item.get("text"), skill=skill)
            for item in output
            if isinstance(item, Content) or isinstance(item, dict) and item.get("type") == "text"
        )
    if isinstance(output, dict):
        retrieval = output.get("application/vnd.ms-workiq.retrieval")
        return (
            output.get("isError") is True or bool(output.get("error"))
            or (isinstance(retrieval, dict) and retrieval.get("stoppedReason") == "error")
        )
    return False
