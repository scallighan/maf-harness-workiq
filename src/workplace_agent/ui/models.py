from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Reference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=512)
    url: str = Field(max_length=4096)
    drive_id: str | None = Field(default=None, max_length=256)
    item_id: str | None = Field(default=None, max_length=256)
    page: int | None = Field(default=None, ge=1, le=100_000, strict=True)
    slide: int | None = Field(default=None, ge=1, le=100_000, strict=True)
    location_verified: Literal[False] = False

    @field_validator("url")
    @classmethod
    def valid_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or any(ord(character) < 32 for character in value)
        ):
            raise ValueError("Source links must be HTTPS URLs without embedded credentials.")
        return value

    @field_validator("drive_id", "item_id")
    @classmethod
    def valid_identifier(cls, value: str | None) -> str | None:
        if value is not None and (
            not value or any(character in value for character in "/\\?#%")
            or any(ord(character) < 32 for character in value)
        ):
            raise ValueError("Use an opaque Graph drive/item ID, not a path.")
        return value


class ToolActivity(BaseModel):
    call_id: str
    name: str
    kind: Literal["tool", "skill"] = "tool"
    skill: str | None = None
    status: Literal["running", "completed", "failed", "cancelled"] = "running"
    started_at: str
    duration_ms: int | None = None


class RunRecord(BaseModel):
    id: str
    session_id: str
    prompt_preview: str
    status: Literal["running", "completed", "incomplete", "failed", "cancelled"] = "running"
    started_at: str
    response_id: str | None = None
    assistant_message_ids: list[str] = Field(default_factory=list)
    activity: list[ToolActivity] = Field(default_factory=list)


class RenameSession(BaseModel):
    title: str = Field(min_length=1, max_length=100)

    @field_validator("title")
    @classmethod
    def nonempty_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("A session title cannot be blank.")
        return value


class PreviewRequest(BaseModel):
    reference: Reference
