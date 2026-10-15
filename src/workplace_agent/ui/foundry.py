from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from urllib.parse import quote, unquote, urlsplit

import httpx
from fastapi import HTTPException
from pydantic import ValidationError

from .config import UIConfig
from .models import Reference, RunRecord
from .payloads import payload_text
from .references import references_from_text, sharepoint_file, validate_preview_url


def upstream_error(response: httpx.Response) -> None:
    if response.is_success:
        return
    if response.status_code in (401, 403):
        raise HTTPException(
            status_code=403, detail="The signed-in user does not have access to this service or file.",
        )
    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="The conversation or referenced file is unavailable.")
    if response.status_code == 429:
        raise HTTPException(status_code=429, detail="The upstream service is busy; retry shortly.")
    raise HTTPException(
        status_code=502, detail=f"Upstream service request failed (HTTP {response.status_code}).",
    )


class FoundryGateway:
    def __init__(self, config: UIConfig, client: httpx.AsyncClient) -> None:
        self.client = client
        self.openai_url = f"{config.project_endpoint}/openai/v1"
        self.agent_url = (
            f"{config.project_endpoint}/agents/{quote(config.agent_name, safe='')}"
            "/endpoint/protocols/openai"
        )

    async def create_conversation(self, token: str) -> str:
        response = await self.client.post(
            f"{self.agent_url}/conversations", params={"api-version": "v1"},
            headers={"Authorization": f"Bearer {token}"}, json={},
        )
        upstream_error(response)
        identifier = response.json().get("id")
        if not isinstance(identifier, str) or not identifier:
            raise HTTPException(status_code=502, detail="Foundry returned an invalid conversation ID.")
        return identifier

    async def has_legacy_history(self, token: str, conversation_id: str) -> bool:
        response = await self.client.get(
            f"{self.openai_url}/conversations/{quote(conversation_id, safe='')}/items",
            params={"limit": 1}, headers={"Authorization": f"Bearer {token}"},
        )
        upstream_error(response)
        items = response.json().get("data")
        if not isinstance(items, list):
            raise HTTPException(status_code=502, detail="Foundry returned invalid conversation items.")
        return bool(items)

    async def messages(self, token: str, conversation_id: str, *, legacy: bool = False) -> list[dict]:
        messages: list[dict] = []
        cursor: str | None = None
        for _ in range(20):
            parameters: dict[str, str | int] = {"order": "asc", "limit": 100}
            if not legacy:
                parameters["api-version"] = "v1"
            if cursor:
                parameters["after"] = cursor
            response = await self.client.get(
                f"{self.openai_url if legacy else self.agent_url}"
                f"/conversations/{quote(conversation_id, safe='')}/items",
                headers={"Authorization": f"Bearer {token}"}, params=parameters,
            )
            upstream_error(response)
            payload = response.json()
            items = payload.get("data")
            if not isinstance(items, list):
                raise HTTPException(status_code=502, detail="Foundry returned invalid conversation items.")
            for item in items:
                if item.get("type") != "message" or item.get("role") not in ("user", "assistant"):
                    continue
                content = item.get("content", [])
                text = "".join(
                    block.get("refusal", "") if block.get("type") == "refusal" else block.get("text", "")
                    for block in content if block.get("type") in ("input_text", "output_text", "refusal")
                )
                if text:
                    annotations = [
                        annotation for block in content for annotation in block.get("annotations", [])
                    ]
                    messages.append({
                        "id": item["id"], "role": item["role"], "content": text,
                        "references": [
                            reference.model_dump(mode="json")
                            for reference in references_from_text(text, annotations)
                        ] if item["role"] == "assistant" else [],
                    })
            if not payload.get("has_more"):
                return messages
            if not items or not isinstance(items[-1].get("id"), str):
                raise HTTPException(status_code=502, detail="Foundry returned an invalid pagination cursor.")
            cursor = items[-1]["id"]
        raise HTTPException(
            status_code=413, detail="This conversation is too large to load; start a new session.",
        )

    async def delete_conversation(self, token: str, conversation_id: str, *, legacy: bool = False) -> None:
        response = await self.client.delete(
            f"{self.openai_url if legacy else self.agent_url}"
            f"/conversations/{quote(conversation_id, safe='')}",
            params={} if legacy else {"api-version": "v1"},
            headers={"Authorization": f"Bearer {token}"},
        )
        if response.status_code == 404:
            return
        upstream_error(response)

    async def tool_details(self, token: str, record: RunRecord, call_id: str) -> dict:
        if not record.response_id or record.status == "running":
            raise HTTPException(
                status_code=409, detail="Tool payloads are available after the prompt finishes.",
            )
        async with self.client.stream(
            "GET", f"{self.agent_url}/responses/{quote(record.response_id, safe='')}",
            params={"api-version": "v1"}, headers={"Authorization": f"Bearer {token}"},
        ) as response:
            upstream_error(response)
            chunks = []
            size = 0
            async for chunk in response.aiter_bytes(chunk_size=64_000):
                size += len(chunk)
                if size > 10_000_000:
                    raise HTTPException(
                        status_code=413, detail="This stored tool response is too large to display.",
                    )
                chunks.append(chunk)
        try:
            payload = json.loads(b"".join(chunks))
        except json.JSONDecodeError as error:
            raise HTTPException(
                status_code=502, detail="Foundry returned invalid stored tool data.",
            ) from error
        if not isinstance(payload, dict):
            raise HTTPException(status_code=502, detail="Foundry returned invalid stored tool data.")
        items = payload.get("output")
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise HTTPException(status_code=502, detail="Foundry returned invalid stored tool data.")
        calls = [
            item for item in items if item.get("type") == "function_call" and item.get("call_id") == call_id
        ]
        if len(calls) != 1 or not isinstance(calls[0].get("arguments"), str):
            raise HTTPException(
                status_code=404, detail="This tool call is unavailable in the stored response.",
            )
        results = [
            item for item in items
            if item.get("type") == "function_call_output" and item.get("call_id") == call_id
        ]
        if len(results) > 1 or (results and "output" not in results[0]):
            raise HTTPException(status_code=502, detail="Foundry returned ambiguous tool results.")
        inputs, input_truncated = payload_text(calls[0]["arguments"])
        outputs, output_truncated = payload_text(results[0]["output"]) if results else (None, False)
        return {
            "input": inputs, "output": outputs,
            "input_truncated": input_truncated, "output_truncated": output_truncated,
        }

    async def stream(self, token: str, conversation_id: str, prompt: str) -> AsyncGenerator[dict]:
        async with self.client.stream(
            "POST", f"{self.agent_url}/responses",
            params={"api-version": "v1"}, headers={"Authorization": f"Bearer {token}"},
            json={"input": prompt, "conversation": conversation_id, "store": True, "stream": True},
        ) as response:
            if not response.is_success:
                await response.aread()
                upstream_error(response)
            data: list[str] = []
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    data.append(line[5:].lstrip())
                    if sum(map(len, data)) > 10_000_000:
                        raise HTTPException(
                            status_code=502, detail="Foundry returned an oversized stream event.",
                        )
                elif not line and data:
                    raw = "\n".join(data)
                    data.clear()
                    if raw == "[DONE]":
                        continue
                    payload = json.loads(raw)
                    if not isinstance(payload, dict) or not isinstance(payload.get("type"), str):
                        raise HTTPException(
                            status_code=502, detail="Foundry returned an invalid stream event.",
                        )
                    yield payload
            if data and "\n".join(data) != "[DONE]":
                payload = json.loads("\n".join(data))
                if not isinstance(payload, dict) or not isinstance(payload.get("type"), str):
                    raise HTTPException(status_code=502, detail="Foundry returned an invalid stream event.")
                yield payload


async def resolve_reference(client: httpx.AsyncClient, token: str, reference: Reference) -> Reference:
    try:
        source = sharepoint_file(reference.url)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    headers = {"Authorization": f"Bearer {token}"}
    site_address = (
        f"{source.hostname}:{quote(source.site_path, safe='/')}"
        if source.site_path else source.hostname
    )
    response = await client.get(
        f"https://graph.microsoft.com/v1.0/sites/{site_address}",
        headers=headers, params={"$select": "id"},
    )
    upstream_error(response)
    site_id = response.json().get("id")
    if not isinstance(site_id, str) or not site_id:
        raise HTTPException(status_code=502, detail="Graph did not return a site ID.")
    drives_url = f"https://graph.microsoft.com/v1.0/sites/{quote(site_id, safe='')}/drives"
    next_url = drives_url
    for page in range(10):
        response = await client.get(
            next_url, headers=headers, params={"$select": "id,webUrl", "$top": 100} if page == 0 else None,
        )
        upstream_error(response)
        payload = response.json()
        drives = payload.get("value")
        if not isinstance(drives, list):
            raise HTTPException(status_code=502, detail="Graph returned invalid document libraries.")
        for drive in drives:
            if (
                not isinstance(drive, dict)
                or not isinstance(drive.get("webUrl"), str) or not isinstance(drive.get("id"), str)
            ):
                raise HTTPException(status_code=502, detail="Graph returned an invalid document library.")
            library = urlsplit(drive["webUrl"])
            library_path = unquote(library.path).rstrip("/")
            if library_path.lower().endswith("/forms/allitems.aspx"):
                library_path = library_path[:-len("/Forms/AllItems.aspx")]
            if (
                library.hostname != source.hostname
                or library.scheme != "https" or library.username or library.password
                or library.port not in (None, 443)
            ):
                continue
            if source.unique_id:
                # Graph accepts a SharePoint list item's unique GUID as an item lookup ID.
                item_path = f"items/{source.unique_id}"
            elif source.file_path and source.file_path.casefold().startswith(f"{library_path}/".casefold()):
                relative_path = source.file_path[len(library_path):].lstrip("/")
                item_path = f"root:/{quote(relative_path, safe='/')}"
            else:
                continue
            response = await client.get(
                f"https://graph.microsoft.com/v1.0/drives/{quote(drive['id'], safe='')}/{item_path}",
                headers=headers, params={"$select": "id,file"},
            )
            if source.unique_id and response.status_code == 404:
                continue
            upstream_error(response)
            item = response.json()
            if not isinstance(item.get("file"), dict):
                raise HTTPException(status_code=422, detail="The referenced item is not a previewable file.")
            try:
                return Reference.model_validate({
                    **reference.model_dump(), "drive_id": drive["id"], "item_id": item["id"],
                })
            except (ValidationError, KeyError) as error:
                raise HTTPException(
                    status_code=502, detail="Graph returned invalid file identifiers.",
                ) from error
        next_link = payload.get("@odata.nextLink")
        if next_link is None:
            raise HTTPException(
                status_code=404,
                detail="The referenced file or document library is unavailable to this user.",
            )
        if not isinstance(next_link, str):
            raise HTTPException(status_code=502, detail="Graph returned an invalid library pagination link.")
        parsed = urlsplit(next_link)
        if (
            parsed.scheme != "https" or parsed.hostname != "graph.microsoft.com"
            or parsed.port not in (None, 443) or parsed.username or parsed.password
            or unquote(parsed.path) != unquote(urlsplit(drives_url).path) or parsed.fragment
        ):
            raise HTTPException(
                status_code=502, detail="Graph returned an unsupported library pagination link.",
            )
        next_url = next_link
    raise HTTPException(status_code=413, detail="Too many document libraries to resolve this source.")


async def preview_reference(client: httpx.AsyncClient, token: str, reference: Reference) -> dict:
    if not reference.drive_id or not reference.item_id:
        reference = await resolve_reference(client, token, reference)
    drive_id, item_id = reference.drive_id, reference.item_id
    if not drive_id or not item_id:
        raise HTTPException(status_code=502, detail="Graph did not resolve the file identifiers.")
    url = (
        f"https://graph.microsoft.com/v1.0/drives/{quote(drive_id, safe='')}"
        f"/items/{quote(item_id, safe='')}/preview"
    )
    body = {}
    position = reference.slide or reference.page
    if position is not None:
        body["page"] = str(position)
    response = await client.post(url, headers={"Authorization": f"Bearer {token}"}, json=body)
    warning = None
    if response.status_code == 400 and position is not None:
        response = await client.post(url, headers={"Authorization": f"Bearer {token}"}, json={})
        warning = (
            "The file viewer rejected the requested position. "
            "The preview opens at its default location."
        )
    if response.status_code in (400, 415):
        raise HTTPException(
            status_code=422, detail="This file cannot be previewed. Use Open original.",
        )
    upstream_error(response)
    payload = response.json()
    result: dict = {"warning": warning, "position_verified": False}
    try:
        if isinstance(payload.get("getUrl"), str):
            result["get_url"] = validate_preview_url(payload["getUrl"])
        elif isinstance(payload.get("postUrl"), str) and isinstance(payload.get("postParameters"), str):
            result["post_url"] = validate_preview_url(payload["postUrl"])
            result["post_parameters"] = payload["postParameters"]
        else:
            raise ValueError("Graph did not return a supported preview URL.")
    except ValueError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    return result
