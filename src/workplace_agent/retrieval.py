from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Any, Literal

from agent_framework import Content
from agent_framework.exceptions import ToolExecutionException
from agent_framework_foundry_hosting import FoundryToolbox
from azure.core.credentials_async import AsyncTokenCredential

from .execution import tool_output_failed
from .progress import SourceSearchProgress


class ParallelWorkIQToolbox(FoundryToolbox):
    def __init__(
        self, credential: AsyncTokenCredential, *, url: str, progress: SourceSearchProgress | None = None,
    ) -> None:
        super().__init__(credential, url=url, load_prompts=False)
        self.retrieval_credential = credential
        self.retrieval_url = url
        self.progress = progress

    async def call_tool(self, tool_name: str, **kwargs: Any) -> str | list[Content]:
        capabilities = kwargs.get("capabilities")
        if not (
            tool_name == "workiq___retrieve"
            and isinstance(capabilities, list) and len(capabilities) == 2
            and all(isinstance(capability, dict) for capability in capabilities)
            and {capability.get("name") for capability in capabilities} == {"OneDriveAndSharePoint", "Email"}
        ):
            return await super().call_tool(tool_name, **kwargs)

        async def search(capability: dict[str, Any]) -> list[Content]:
            source_name = capability["name"]
            if source_name not in ("Email", "OneDriveAndSharePoint"):
                raise ValueError("Unsupported parallel search source.")
            search = self.progress.start(source_name) if self.progress else None
            status: Literal["completed", "failed", "cancelled"] = "failed"
            arguments = deepcopy(kwargs)
            arguments["capabilities"] = [deepcopy(capability)]
            try:
                # Separate SDK sessions retain caller-header isolation without serializing the searches.
                async with FoundryToolbox(
                    self.retrieval_credential, url=self.retrieval_url, load_prompts=False,
                ) as source:
                    function = next((tool for tool in source.functions if tool.name == tool_name), None)
                    if function is None:
                        raise ToolExecutionException(
                            "Work IQ retrieval is unavailable for this source search.",
                        )
                    result = await function.invoke(arguments=arguments)
                if not isinstance(result, list) or any(
                    not isinstance(content, Content) for content in result
                ):
                    raise ToolExecutionException("Work IQ returned unsupported retrieval content.")
                status = "failed" if tool_output_failed(result, skill=False) else "completed"
            except asyncio.CancelledError:
                status = "cancelled"
                raise
            finally:
                if self.progress:
                    self.progress.finish(search, status)
            return [
                Content.from_text(
                    f"Search source: {capability['name']}. Citation IDs belong only to this source section.",
                ),
                *result,
            ]

        tasks = [asyncio.create_task(search(capability)) for capability in capabilities]
        try:
            results = await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return [content for result in results for content in result]
