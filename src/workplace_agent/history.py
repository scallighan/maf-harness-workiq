from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Any

from agent_framework import HistoryProvider, Message
from agent_framework_azure_cosmos import CosmosHistoryProvider


class ScopedCosmosHistoryProvider(HistoryProvider):
    def __init__(
        self, provider: CosmosHistoryProvider, *, tenant_id: str,
        project_endpoint: str, user_id: str, platform_session_id: str,
    ) -> None:
        super().__init__("workplace_history")
        if not all((tenant_id, project_endpoint, user_id, platform_session_id)):
            raise ValueError("Cosmos history requires a complete trusted caller scope.")
        self._provider = provider
        self._scope = [tenant_id, project_endpoint, user_id, platform_session_id]

    def partition(self, session_id: str | None) -> str:
        if not session_id:
            raise ValueError("A conversation session ID is required for Cosmos history.")
        identity = json.dumps([*self._scope, session_id], separators=(",", ":"))
        return hashlib.sha256(identity.encode()).hexdigest()

    async def get_messages(
        self, session_id: str | None, *, state: dict[str, Any] | None = None, **kwargs: Any,
    ) -> list[Message]:
        return await self._provider.get_messages(self.partition(session_id))

    async def save_messages(
        self, session_id: str | None, messages: Sequence[Message],
        *, state: dict[str, Any] | None = None, **kwargs: Any,
    ) -> None:
        await self._provider.save_messages(self.partition(session_id), messages)
