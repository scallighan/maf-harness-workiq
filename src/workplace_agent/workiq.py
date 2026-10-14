from __future__ import annotations

import asyncio
import json
import re
from typing import Annotated, Protocol

from agent_framework import tool
from mcp.types import CallToolResult

from .evidence import Blob, Source, find_evidence

DRIVE_PATH = re.compile(r"^/(?:me/drive|drives/[^/?#]+)/items/[^/?#]+$")
ATTACHMENT_PATH = re.compile(r"^/me/messages/[^/?#]+/attachments/[^/?#]+$")
READ_TOOLS = ("ask", "fetch", "get_schema", "search_paths", "fetch_blob")


class WorkIQClient(Protocol):
    async def call_tool(self, name: str, **arguments: object) -> str: ...


def serialize_result(result: CallToolResult) -> str:
    """Keep structuredContent intact, including blobs absent from MCP text blocks."""
    return result.model_dump_json(exclude_none=True)


def unpack_result(raw: str) -> dict:
    result = CallToolResult.model_validate_json(raw)
    if result.isError:
        raise RuntimeError("Work IQ returned a tool error; do not treat it as document evidence.")
    if result.structuredContent is not None:
        return result.structuredContent
    texts = [block.text for block in result.content if block.type == "text"]
    if len(texts) == 1:
        try:
            decoded = json.loads(texts[0])
        except json.JSONDecodeError:
            return {"text": texts[0]}
        if isinstance(decoded, dict):
            return decoded
    return {"text": "\n".join(texts)}


def redact_binary(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: "[binary omitted; use inspect_document]" if key in {
                "base64Content", "contentBytes", "@microsoft.graph.downloadUrl"
            } else redact_binary(child)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [redact_binary(child) for child in value]
    return value


def entity_data(payload: dict) -> dict:
    results = payload.get("results")
    if not isinstance(results, list) or len(results) != 1:
        raise ValueError("Expected one Work IQ fetch result.")
    result = results[0]
    if not isinstance(result, dict) or result.get("statusCode") != 200:
        status = result.get("statusCode") if isinstance(result, dict) else "invalid response"
        raise ValueError(f"Work IQ metadata fetch failed ({status}).")
    data = result.get("data")
    if not isinstance(data, dict):
        raise ValueError("Work IQ metadata response is not an object.")
    return data


def validate_document_path(path: str) -> str:
    if not (DRIVE_PATH.fullmatch(path) or ATTACHMENT_PATH.fullmatch(path)):
        raise ValueError("Use a drive item or /me/messages/{id}/attachments/{id} metadata path.")
    # Reject path traversal, including encoded delimiters and double-encoded input.
    if "%" in path or "\\" in path or any(segment in {".", ".."} for segment in path.split("/")):
        raise ValueError("Document paths must use opaque IDs without encoded delimiters or traversal.")
    return path


class WorkIQ:
    def __init__(self, client: WorkIQClient) -> None:
        self.client = client

    async def call(self, name: str, **arguments: object) -> dict:
        if name not in READ_TOOLS:
            raise ValueError("This agent exposes only read-only Work IQ operations.")
        raw = await self.client.call_tool(name, **arguments)
        if not isinstance(raw, str):
            raise TypeError("Work IQ parser must return the serialized MCP result.")
        return unpack_result(raw)

    async def inspect(self, path: str, phrase: str) -> dict:
        path = validate_document_path(path)
        metadata = entity_data(await self.call("fetch", entityUrls=[path]))
        name = metadata.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("Document metadata does not include a filename.")
        is_drive = bool(DRIVE_PATH.fullmatch(path))
        converted = is_drive and not name.lower().endswith(".pdf")
        if is_drive:
            parent = metadata.get("parentReference") or {}
            source = Source(
                kind="onedrive",
                title=name,
                resource_path=path,
                web_url=metadata.get("webUrl"),
                folder=parent.get("path"),
            )
            blob_args = {"path": f"{path}/content"}
            if converted:
                blob_args["format"] = "pdf"
        else:
            if metadata.get("@odata.type") != "#microsoft.graph.fileAttachment":
                raise ValueError(
                    "Only file attachments are supported; follow reference attachments to OneDrive."
                )
            if not name.lower().endswith(".pdf"):
                raise ValueError(
                    "Email attachment is not a PDF. Native Office attachments have no stable page numbers; "
                    "use an existing OneDrive PDF rendition or open the attachment and cite its section."
                )
            message_path = path.split("/attachments/")[0]
            message = entity_data(await self.call(
                "fetch",
                entityUrls=[f"{message_path}?$select=subject,from,receivedDateTime,webLink"],
            ))
            source = Source(
                kind="email_attachment",
                title=name,
                resource_path=path,
                web_url=message.get("webLink"),
                email_subject=message.get("subject"),
                sender=(message.get("from") or {}).get("emailAddress", {}).get("address"),
                received_at=message.get("receivedDateTime"),
            )
            blob_args = {"path": f"{path}/$value"}
        blob = Blob.model_validate(await self.call("fetch_blob", **blob_args))
        data = blob.pdf_bytes()
        return await asyncio.to_thread(find_evidence, data, source, phrase, converted=converted)

    def tools(self) -> list:
        @tool(approval_mode="never_require")
        async def discover_workplace_documents(
            question: Annotated[str, "Describe the content to locate across OneDrive and email attachments."],
        ) -> str:
            """Find candidates through Work IQ. Results are leads, NOT verified page evidence."""
            result = await self.call(
                "ask",
                question=(
                    "Find documents matching this request across OneDrive/SharePoint and email attachments. "
                    "Return source URLs, filenames, drive/item or message/attachment IDs when available; "
                    "include email subject, sender and date. Do not invent IDs or page numbers. Request: "
                    + question
                ),
            )
            return json.dumps(redact_binary(result))

        @tool(approval_mode="never_require")
        async def search_workiq_paths(
            filter: Annotated[str, "Prefix or regex for available Work IQ API paths."],
        ) -> str:
            """Discover tenant-supported paths before trying document or attachment endpoints."""
            return json.dumps(await self.call("search_paths", filter=filter))

        @tool(approval_mode="never_require")
        async def get_workiq_schema(
            path: Annotated[str, "Relative API path whose read schema is needed."],
        ) -> str:
            """Inspect read schemas and supported resource metadata."""
            return json.dumps(await self.call("get_schema", path=path, operationType="fetch"))

        @tool(approval_mode="never_require")
        async def fetch_workiq_metadata(
            paths: Annotated[list[str], "Relative read resource paths, including queries; at most 10."],
        ) -> str:
            """Read metadata, find IDs, search drives/messages, and list attachments. No binary output."""
            if not 1 <= len(paths) <= 10 or any(
                not p.startswith("/") or p.startswith("//") or p.endswith(("/content", "/$value"))
                for p in paths
            ):
                raise ValueError("Supply 1-10 relative metadata paths, not binary content paths.")
            return json.dumps(redact_binary(await self.call("fetch", entityUrls=paths)))

        @tool(approval_mode="never_require")
        async def inspect_document(
            resource_path: Annotated[str, "Work IQ metadata path: drive item or email file attachment."],
            phrase: Annotated[str, "Literal phrase (3-500 characters) to locate within the PDF."],
        ) -> str:
            """Retrieve bytes privately; return verified PDF pages, quotes and source location."""
            return json.dumps(await self.inspect(resource_path, phrase))

        return [
            discover_workplace_documents, search_workiq_paths, get_workiq_schema,
            fetch_workiq_metadata, inspect_document,
        ]
