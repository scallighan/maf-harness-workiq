import base64
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp.types import CallToolResult, TextContent

from workplace_agent.workiq import WorkIQ, serialize_result, unpack_result, validate_document_path


def result(payload: dict) -> str:
    return serialize_result(CallToolResult(content=[], structuredContent=payload))


def metadata(data: dict) -> str:
    return result({"results": [{"statusCode": 200, "data": data}]})


def blob(data: bytes) -> str:
    return result({
        "statusCode": 200, "contentType": "application/pdf", "sizeBytes": len(data),
        "base64Content": base64.b64encode(data).decode(),
    })


@pytest.mark.parametrize("path", [
    "/me/drive/items/../content", "/me/drive/items/%2e%2e", "/me/drive/items/1?x=1",
    "https://example.com/file", "/users/someone/messages/1/attachments/2",
])
def test_document_path_rejected(path: str) -> None:
    with pytest.raises(ValueError):
        validate_document_path(path)


async def test_drive_conversion_and_page_evidence(pdf_bytes: bytes) -> None:
    toolbox = MagicMock()
    toolbox.call_tool = AsyncMock(side_effect=[
        metadata({"name": "Plan.docx", "webUrl": "https://example.sharepoint.com/Plan.docx",
                  "parentReference": {"path": "/drive/root:/Planning"}}),
        blob(pdf_bytes),
    ])
    evidence = await WorkIQ(toolbox).inspect("/drives/d/items/i", "renewal deadline")
    assert evidence["matches"][0]["page_number"] == 2
    assert evidence["pagination"] == "converted_pdf"
    toolbox.call_tool.assert_any_await("fetch_blob", path="/drives/d/items/i/content", format="pdf")


async def test_email_attachment_returns_message_navigation(pdf_bytes: bytes) -> None:
    toolbox = MagicMock()
    toolbox.call_tool = AsyncMock(side_effect=[
        metadata({"name": "Plan.pdf", "@odata.type": "#microsoft.graph.fileAttachment"}),
        metadata({"subject": "Renewal", "from": {"emailAddress": {"address": "sender@example.com"}},
                  "receivedDateTime": "2026-10-01T12:00:00Z", "webLink": "https://outlook.office.com/mail/id/m"}),
        blob(pdf_bytes),
    ])
    evidence = await WorkIQ(toolbox).inspect("/me/messages/m/attachments/a", "renewal deadline")
    assert evidence["source"]["email_subject"] == "Renewal"
    assert evidence["source"]["sender"] == "sender@example.com"
    assert evidence["matches"][0]["page_number"] == 2
    toolbox.call_tool.assert_any_await("fetch_blob", path="/me/messages/m/attachments/a/$value")


async def test_non_pdf_email_fails_without_blob_fetch() -> None:
    toolbox = MagicMock()
    toolbox.call_tool = AsyncMock(return_value=metadata({
        "name": "Plan.docx", "@odata.type": "#microsoft.graph.fileAttachment",
    }))
    with pytest.raises(ValueError, match="not a PDF"):
        await WorkIQ(toolbox).inspect("/me/messages/m/attachments/a", "deadline")
    assert toolbox.call_tool.await_count == 1


async def test_tool_error_propagates() -> None:
    toolbox = MagicMock()
    toolbox.call_tool = AsyncMock(return_value=serialize_result(CallToolResult(
        isError=True, content=[TextContent(type="text", text="Consent required")],
    )))
    with pytest.raises(RuntimeError, match="tool error"):
        await WorkIQ(toolbox).call("ask", question="deadline")
    with pytest.raises(ValueError, match="read-only"):
        await WorkIQ(toolbox).call("delete_entity", entityUrl="/me/drive/items/1")


async def test_metadata_redacts_binary_and_download_urls() -> None:
    toolbox = MagicMock()
    toolbox.call_tool = AsyncMock(return_value=result({"results": [{
        "data": {"name": "Plan.pdf", "contentBytes": "private-bytes",
                 "@microsoft.graph.downloadUrl": "https://example.com/secret"},
    }]}))
    tool = next(t for t in WorkIQ(toolbox).tools() if t.name == "fetch_workiq_metadata")
    raw = await tool.func(paths=["/me/messages/m/attachments"])
    assert "private-bytes" not in raw
    assert "https://example.com/secret" not in raw
    assert json.loads(raw)["results"][0]["data"]["name"] == "Plan.pdf"
    with pytest.raises(ValueError, match="metadata paths"):
        await tool.func(paths=["/me/drive/items/1/content"])


def test_text_json_and_structured_content_unpacking() -> None:
    assert unpack_result(result({"base64Content": "abc"})) == {"base64Content": "abc"}
    raw = serialize_result(CallToolResult(content=[TextContent(type="text", text='{"answer": "lead"}')]))
    assert unpack_result(raw) == {"answer": "lead"}
