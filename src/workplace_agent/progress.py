from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from agent_framework import (
    AgentContext,
    AgentMiddleware,
    AgentResponse,
    AgentResponseUpdate,
    Content,
    ResponseStream,
)

from .execution import TIMING_KEY, ExecutionTiming, ToolTimingRecorder

logger = logging.getLogger(__name__)
SOURCE_SEARCH_PREFIX = "workiq_source_search___"
SearchSource = Literal["Email", "OneDriveAndSharePoint"]


@dataclass(frozen=True)
class SourceSearch:
    call_id: str
    source: SearchSource
    started_at: datetime
    started_ns: int


@dataclass(frozen=True)
class StreamEnd:
    response: AgentResponse | None = None
    error: BaseException | None = None


class SourceSearchProgress:
    def __init__(self, recorder: ToolTimingRecorder) -> None:
        self.recorder = recorder
        self.updates: asyncio.Queue[AgentResponseUpdate | StreamEnd] = asyncio.Queue()

    def start(self, source: SearchSource) -> SourceSearch | None:
        parent = self.recorder.active_call_id.get()
        if parent is None:
            return None
        search = SourceSearch(f"source_{uuid4().hex}", source, datetime.now(UTC), time.perf_counter_ns())
        self.updates.put_nowait(AgentResponseUpdate(role="assistant", contents=[
            Content.from_function_call(
                search.call_id, f"{SOURCE_SEARCH_PREFIX}{source}",
                arguments={"source": source, "parent_call_id": parent},
            ),
        ]))
        return search

    def finish(
        self, search: SourceSearch | None, status: Literal["completed", "failed", "cancelled"],
    ) -> None:
        if search is None:
            return
        timing = ExecutionTiming(
            call_id=search.call_id, name=f"{SOURCE_SEARCH_PREFIX}{search.source}",
            started_at=search.started_at,
            duration_ms=(time.perf_counter_ns() - search.started_ns) // 1_000_000,
            failed=status != "completed",
        )
        self.updates.put_nowait(AgentResponseUpdate(role="tool", contents=[
            Content.from_function_result(search.call_id, result={
                TIMING_KEY: timing.model_dump(mode="json"), "output": {"status": status},
            }),
        ]))
        logger.info(
            "Source search finished: source=%s status=%s elapsed_ms=%d",
            search.source, status, timing.duration_ms,
        )


class SourceProgressOutput(AgentMiddleware):
    def __init__(self, progress: SourceSearchProgress) -> None:
        self.progress = progress

    async def process(self, context: AgentContext, call_next: Callable[[], Awaitable[None]]) -> None:
        await call_next()
        if not context.stream or not isinstance(context.result, ResponseStream):
            return
        original = context.result
        final: AgentResponse | None = None

        async def produce() -> None:
            # One producer task owns all SDK pulls and cleanup so its ContextVars remain valid.
            try:
                async with original:
                    async for update in original:
                        self.progress.updates.put_nowait(update)
                    response = await original.get_final_response()
            except BaseException as error:
                self.progress.updates.put_nowait(StreamEnd(error=error))
            else:
                self.progress.updates.put_nowait(StreamEnd(response=response))

        async def updates():
            nonlocal final
            producer = asyncio.create_task(produce())
            try:
                while True:
                    update = await self.progress.updates.get()
                    if isinstance(update, StreamEnd):
                        if update.error is not None:
                            raise update.error
                        final = update.response
                        return
                    yield update
            finally:
                producer.cancel()
                await asyncio.gather(producer, return_exceptions=True)

        async def finalize(_: Sequence[AgentResponseUpdate]) -> AgentResponse:
            # Diagnostic source events go to the caller, never model input or agent history.
            if final is None:
                raise RuntimeError("The source progress stream ended without an agent response.")
            return final

        context.result = ResponseStream(updates(), finalizer=finalize)
