from __future__ import annotations

import json
import logging
from contextlib import AsyncExitStack
from types import TracebackType

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from .auth import WORKIQ_ENDPOINT, WorkIQAuth, redact_tool_error
from .workiq import READ_TOOLS, serialize_result

logger = logging.getLogger(__name__)


def direct_http_client(
    headers: dict[str, str] | None = None, timeout: httpx.Timeout | None = None,
    auth: httpx.Auth | None = None,
) -> httpx.AsyncClient:
    return httpx.AsyncClient(headers=headers, timeout=timeout, auth=auth, follow_redirects=False)


class DirectWorkIQClient:
    def __init__(self, auth: httpx.Auth, *, show_tool_errors: bool = False) -> None:
        self._auth = auth
        self._show_tool_errors = show_tool_errors
        self._stack = AsyncExitStack()
        self._session: ClientSession | None = None

    async def __aenter__(self) -> DirectWorkIQClient:
        logger.info("Connecting to Work IQ MCP: %s", WORKIQ_ENDPOINT)
        try:
            read, write, _ = await self._stack.enter_async_context(streamablehttp_client(
                WORKIQ_ENDPOINT, auth=self._auth, timeout=120, sse_read_timeout=120,
                httpx_client_factory=direct_http_client,
            ))
            self._session = await self._stack.enter_async_context(
                ClientSession(read, write)
            )
            logger.info("Initializing Work IQ MCP session.")
            await self._session.initialize()
            logger.info("Work IQ MCP session ready.")
        except BaseException:
            await self._stack.aclose()
            raise
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._session = None
        await self._stack.__aexit__(exc_type, exc_value, traceback)

    async def call_tool(self, name: str, **arguments: object) -> str:
        if name not in READ_TOOLS:
            raise ValueError("Only read-only Work IQ tools are permitted.")
        if self._session is None:
            raise RuntimeError("Direct Work IQ MCP is not connected.")
        logger.info("Calling Work IQ tool %s at %s.", name, WORKIQ_ENDPOINT)
        result = await self._session.call_tool(name, arguments)
        logger.info("Work IQ tool %s returned (is_error=%s).", name, result.isError)
        if result.isError and self._show_tool_errors:
            text = "\n".join(block.text for block in result.content if block.type == "text")
            if text:
                text = (
                    self._auth.redact_tool_error(text)
                    if isinstance(self._auth, WorkIQAuth) else redact_tool_error(text)
                )
                logger.warning("Work IQ tool %s error detail: %s", name, json.dumps(text[:2000]))
            else:
                logger.warning(
                    "Work IQ tool %s failed without text details; structured payload withheld.", name,
                )
        return serialize_result(result)
