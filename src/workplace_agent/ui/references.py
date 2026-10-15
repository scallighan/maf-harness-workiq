from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import UUID

from pydantic import ValidationError

from .models import Reference

REFERENCE_BLOCK = re.compile(r"```workiq_references\s*\n([\s\S]*?)\n```")
PREVIEW_DOMAINS = (
    "sharepoint.com", "sharepoint-df.com", "officeapps.live.com",
    "office.com", "onedrive.com", "onedrive.live.com", "microsoft365.com",
)


@dataclass(frozen=True)
class SharePointFile:
    hostname: str
    site_path: str
    file_path: str | None
    unique_id: str | None = None


def sharepoint_file(value: str) -> SharePointFile:
    parsed = urlsplit(value)
    hostname = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https" or parsed.username or parsed.password
        or parsed.port not in (None, 443)
        or not hostname.endswith((".sharepoint.com", ".sharepoint-df.com"))
    ):
        raise ValueError("This source has no file IDs or supported SharePoint file URL. Use Open original.")
    path = unquote(parsed.path)
    if (
        path.startswith("//") or "\\" in path or any(ord(character) < 32 for character in path)
        or any(segment in {".", ".."} for segment in path.split("/"))
    ):
        raise ValueError("Invalid SharePoint file path.")
    viewer_suffix = "/_layouts/15/doc.aspx"
    if path.lower().endswith(viewer_suffix):
        targets = [
            value for key, values in parse_qs(parsed.query, keep_blank_values=True).items()
            if key.lower() == "sourcedoc" for value in values
        ]
        if len(targets) != 1:
            raise ValueError("This SharePoint viewer link does not identify one file. Use Open original.")
        try:
            unique_id = str(UUID(targets[0]))
        except ValueError as error:
            raise ValueError(
                "This SharePoint viewer link has an invalid file ID. Use Open original.",
            ) from error
        return SharePointFile(hostname, path[:-len(viewer_suffix)], None, unique_id)
    if path.lower().endswith("/forms/allitems.aspx"):
        targets = parse_qs(parsed.query).get("id", [])
        if len(targets) != 1:
            raise ValueError("This library link does not identify one file. Use its file-specific link.")
        file_path = targets[0]
        library_path = path[:-len("/Forms/AllItems.aspx")].rstrip("/")
        if not file_path.casefold().startswith(f"{library_path}/".casefold()):
            raise ValueError("The referenced file is outside this library link.")
        site_path = library_path.rsplit("/", 1)[0]
    else:
        if "/_layouts/" in path.lower() or path.lower().endswith(".aspx"):
            raise ValueError(
                "This SharePoint viewer link has no file path. Use a file-specific library link.",
            )
        file_path = path
        segments = path.strip("/").split("/")
        site_path = (
            f"/{segments[0]}/{segments[1]}"
            if len(segments) >= 3 and segments[0].lower() in {"sites", "teams", "personal"}
            else ""
        )
    if (
        not file_path.startswith("/") or file_path.startswith("//")
        or "\\" in file_path or any(ord(character) < 32 for character in file_path)
        or any(segment in {".", ".."} for segment in file_path.split("/"))
        or not file_path.rsplit("/", 1)[-1]
    ):
        raise ValueError("Invalid SharePoint file path.")
    return SharePointFile(hostname, site_path, file_path)


def references_from_text(text: str, annotations: Iterable[dict] = ()) -> list[Reference]:
    references: dict[tuple[str, int | None, int | None], Reference] = {}
    for match in REFERENCE_BLOCK.finditer(text):
        try:
            values = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if not isinstance(values, list) or len(values) > 50:
            continue
        for value in values:
            try:
                reference = Reference.model_validate(value)
            except ValidationError:
                continue
            references[(reference.url, reference.page, reference.slide)] = reference
    for annotation in annotations:
        if annotation.get("type") != "url_citation":
            continue
        try:
            reference = Reference(title=annotation.get("title") or "Source", url=annotation["url"])
        except (ValidationError, KeyError):
            continue
        if not any(key[0] == reference.url for key in references):
            references[(reference.url, None, None)] = reference
    return list(references.values())


def validate_preview_url(value: str) -> str:
    parsed = urlsplit(value)
    hostname = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https" or parsed.username or parsed.password
        or parsed.port not in (None, 443)
        or not any(hostname == domain or hostname.endswith(f".{domain}") for domain in PREVIEW_DOMAINS)
    ):
        raise ValueError("Graph returned an unsupported document-preview host.")
    return value
