import asyncio
import json
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock

import pytest
from agent_framework import AgentResponseUpdate, Content, FunctionTool
from agent_framework.exceptions import ToolExecutionException
from agent_framework_foundry_hosting import FoundryToolbox

from workplace_agent import retrieval
from workplace_agent.execution import ToolTimingRecorder, tool_output_failed, unwrap_tool_output
from workplace_agent.progress import SourceSearchProgress
from workplace_agent.retrieval import ParallelWorkIQToolbox

MIME = "application/vnd.ms-workiq.retrieval"


@pytest.mark.parametrize("failed", [False, True])
async def test_file_and_email_retrieval_really_overlap_and_preserve_scoped_payloads(
    monkeypatch: pytest.MonkeyPatch, failed: bool,
) -> None:
    both_started = asyncio.Event()
    calls = []
    closed = []
    credential = MagicMock()

    class Source:
        def __init__(self, supplied_credential, **kwargs):
            assert supplied_credential is credential
            assert kwargs == {"url": "https://example.services.ai.azure.com/toolbox", "load_prompts": False}

            async def request(query: str, strategy: str, capabilities: list[dict]) -> list[Content]:
                return await self.call_tool(
                    "workiq___retrieve", query=query, strategy=strategy, capabilities=capabilities,
                )

            self.functions = [FunctionTool(name="workiq___retrieve", func=request)]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            closed.append(self)

        async def call_tool(self, name, **kwargs):
            assert name == "workiq___retrieve"
            calls.append(kwargs)
            if len(calls) == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=1)
            source = kwargs["capabilities"][0]["name"]
            return [Content.from_text(json.dumps({MIME: {
                "markdown": f"Grounding for {source} [^h1]",
                "retrievalHits": [{
                    "id": "h1", "itemId": source, "sensitivityLabel": {"displayName": "Internal"},
                }],
                "stoppedReason": "error" if failed and source == "Email" else "completed",
            }}), additional_properties={"_meta": {"source_label": source}})]

    monkeypatch.setattr(retrieval, "FoundryToolbox", Source)
    arguments = {
        "query": "Synthetic workflow query", "strategy": "grounding",
        "capabilities": [
            {"name": "OneDriveAndSharePoint", "items_by_url": [{"url": "https://contoso.sharepoint.com/sites/Example"}]},
            {"name": "Email", "folders": [{"folder_id": "folder1"}]},
        ],
    }
    original = deepcopy(arguments)
    recorder = ToolTimingRecorder()
    progress = SourceSearchProgress(recorder)
    toolbox = ParallelWorkIQToolbox(
        credential, url="https://example.services.ai.azure.com/toolbox", progress=progress,
    )
    token = recorder.active_call_id.set("parent")
    try:
        result = await toolbox.call_tool("workiq___retrieve", **arguments)
    finally:
        recorder.active_call_id.reset(token)
    assert not isinstance(result, str)
    assert len(result) == 4
    for index, source in ((0, "OneDriveAndSharePoint"), (2, "Email")):
        heading, text = result[index].text, result[index + 1].text
        assert heading and source in heading
        assert text
        payload = json.loads(text)[MIME]
        assert payload["retrievalHits"][0]["itemId"] == source
        assert result[index + 1].additional_properties["_meta"]["source_label"] == source
    assert tool_output_failed(result, skill=False) is failed
    assert arguments == original
    assert all(call["query"] == arguments["query"] and len(call["capabilities"]) == 1 for call in calls)
    assert len(closed) == 2
    updates = [progress.updates.get_nowait() for _ in range(progress.updates.qsize())]
    results = []
    for update in updates:
        assert isinstance(update, AgentResponseUpdate)
        results.extend(content for content in update.contents if content.type == "function_result")
    assert len(results) == 2
    for content in results:
        metadata = json.loads(content.result)["__workiq_tool_execution__"]
        is_email = metadata["name"].endswith("Email")
        output, timing = unwrap_tool_output(
            content.result, call_id=content.call_id, name=metadata["name"],
        )
        expected_failure = failed and is_email
        assert output == {"status": "failed" if expected_failure else "completed"}
        assert timing and timing.failed is expected_failure


@pytest.mark.parametrize("capabilities", [
    None, [], [{"name": "Email"}], [{"name": "Email"}, {"name": "TeamsMessages"}],
])
async def test_other_source_selections_retain_normal_sdk_approval_and_call_behavior(
    monkeypatch: pytest.MonkeyPatch, capabilities: list | None,
) -> None:
    sdk_call = AsyncMock(return_value="unchanged")
    monkeypatch.setattr(FoundryToolbox, "call_tool", sdk_call)
    toolbox = ParallelWorkIQToolbox(MagicMock(), url="https://example.services.ai.azure.com/toolbox")
    result = await toolbox.call_tool("workiq___retrieve", query="Synthetic", capabilities=capabilities)
    assert result == "unchanged"
    sdk_call.assert_awaited_once_with("workiq___retrieve", query="Synthetic", capabilities=capabilities)


async def test_retrieval_transport_or_consent_failure_propagates_and_cleans_up_both_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    both_started = asyncio.Event()
    cancelled = asyncio.Event()
    closed = []
    count = 0
    failure = ToolExecutionException("Synthetic consent or transport failure")

    class Source:
        def __init__(self, *args, **kwargs):
            async def request(query: str, capabilities: list[dict]) -> list[Content]:
                return await self.call_tool("workiq___retrieve", query=query, capabilities=capabilities)

            self.functions = [FunctionTool(name="workiq___retrieve", func=request)]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            closed.append(self)

        async def call_tool(self, name, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                both_started.set()
            await both_started.wait()
            if kwargs["capabilities"][0]["name"] == "Email":
                raise failure
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
            raise AssertionError("The pending search must be cancelled after its sibling fails.")

    monkeypatch.setattr(retrieval, "FoundryToolbox", Source)
    toolbox = ParallelWorkIQToolbox(MagicMock(), url="https://example.services.ai.azure.com/toolbox")
    with pytest.raises(ToolExecutionException) as error:
        await asyncio.wait_for(toolbox.call_tool(
            "workiq___retrieve", query="Synthetic",
            capabilities=[{"name": "OneDriveAndSharePoint"}, {"name": "Email"}],
        ), timeout=1)
    assert error.value is failure
    assert cancelled.is_set()
    assert len(closed) == 2
