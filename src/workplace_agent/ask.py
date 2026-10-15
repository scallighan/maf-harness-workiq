from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

from agent_framework import Content, FunctionInvocationContext, FunctionMiddleware
from agent_framework.exceptions import ToolExecutionException
from pydantic import BaseModel

from .execution import tool_output_failed

logger = logging.getLogger(__name__)
ASK_STATE_KEY = "workiq_ask_context"
DEFAULT_AGENT_ID = "bizchat-as-gpt-scenario"


def valid_identifier(value: object) -> bool:
    return (
        isinstance(value, str) and 0 < len(value) <= 512
        and not any(character.isspace() or ord(character) < 32 for character in value)
    )


def conversation_id_from_result(result: object) -> str:
    if isinstance(result, str):
        texts = [result]
    elif isinstance(result, list) and all(isinstance(item, Content) for item in result):
        texts = [item.text for item in result if item.type == "text" and item.text is not None]
    else:
        raise ToolExecutionException("Work IQ Ask returned unsupported conversation metadata.")
    identifiers: set[str] = set()
    for text in texts:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and "conversationId" in payload:
            identifier = payload["conversationId"]
            if not valid_identifier(identifier):
                raise ToolExecutionException("Work IQ Ask returned an invalid conversation ID.")
            identifiers.add(identifier)
    if len(identifiers) != 1:
        raise ToolExecutionException("Work IQ Ask did not return one unambiguous conversation ID.")
    return identifiers.pop()


class AskSessionContext(FunctionMiddleware):
    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    async def process(
        self, context: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]],
    ) -> None:
        if context.function.name != "workiq___ask":
            await call_next()
            return
        if context.session is None:
            raise ToolExecutionException("Work IQ Ask requires an active conversation session.")
        async with self._lock:
            arguments = (
                context.arguments.model_dump()
                if isinstance(context.arguments, BaseModel) else dict(context.arguments)
            )
            saved = context.session.state.get(ASK_STATE_KEY)
            if ASK_STATE_KEY in context.session.state and (
                not isinstance(saved, dict) or set(saved) != {"agent_id", "conversation_id"}
                or not valid_identifier(saved.get("agent_id"))
                or not valid_identifier(saved.get("conversation_id"))
            ):
                raise ToolExecutionException(
                    "Work IQ Ask continuity is unavailable for this session; start a new session.",
                )
            agent_id = arguments.get("agentId")
            if agent_id is None:
                agent_id = saved["agent_id"] if saved else DEFAULT_AGENT_ID
            if not valid_identifier(agent_id):
                raise ToolExecutionException("Work IQ Ask requires a valid agent ID.")
            if saved and saved["agent_id"] != agent_id:
                raise ToolExecutionException(
                    "Work IQ Ask cannot change agents within this session; start a new session.",
                )
            # Only the runtime's saved Work IQ ID may continue this session, never a model-supplied ID.
            arguments["conversationId"] = saved["conversation_id"] if saved else None
            arguments["agentId"] = agent_id
            context.arguments = arguments
            await call_next()
            if tool_output_failed(context.result, skill=False):
                logger.warning("Work IQ Ask failed; existing conversation context was retained.")
                return
            if not saved:
                # A successful response without usable metadata must not silently start another thread.
                context.session.state[ASK_STATE_KEY] = {"agent_id": agent_id, "conversation_id": None}
            conversation_id = conversation_id_from_result(context.result)
            if saved and conversation_id != saved["conversation_id"]:
                raise ToolExecutionException(
                    "Work IQ Ask returned a different conversation ID; existing context was retained.",
                )
            context.session.state[ASK_STATE_KEY] = {
                "agent_id": agent_id, "conversation_id": conversation_id,
            }
            logger.info("Work IQ Ask conversation context %s.", "reused" if saved else "established")
