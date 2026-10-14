from __future__ import annotations

import base64
import binascii
import hashlib
import re
from io import BytesIO
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pypdf import PdfReader
from pypdf.errors import PdfReadError

MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
MAX_PAGES = 500
MAX_MATCHES = 12


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["onedrive", "email_attachment"]
    title: str
    resource_path: str
    web_url: str | None = None
    folder: str | None = None
    email_subject: str | None = None
    sender: str | None = None
    received_at: str | None = None

    @field_validator("web_url")
    @classmethod
    def safe_url(cls, value: str | None) -> str | None:
        if value is not None:
            parsed = urlsplit(value)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("Source link must be HTTPS without credentials.")
        return value


class Blob(BaseModel):
    statusCode: int
    contentType: str
    sizeBytes: int = Field(ge=0, le=MAX_DOCUMENT_BYTES)
    base64Content: str = Field(max_length=((MAX_DOCUMENT_BYTES + 2) // 3) * 4)
    blobName: str | None = None

    def pdf_bytes(self) -> bytes:
        if self.statusCode != 200:
            raise ValueError(f"Work IQ document fetch failed (HTTP {self.statusCode}).")
        if self.contentType.split(";")[0].strip().lower() != "application/pdf":
            raise ValueError("Page verification requires a PDF; this content is not a PDF.")
        try:
            data = base64.b64decode(self.base64Content, validate=True)
        except binascii.Error as exc:
            raise ValueError("Work IQ returned invalid Base64 document content.") from exc
        if len(data) != self.sizeBytes or len(data) > MAX_DOCUMENT_BYTES:
            raise ValueError("Work IQ document size does not match the payload or exceeds the limit.")
        if not data.startswith(b"%PDF-"):
            raise ValueError("The document payload does not have a PDF signature.")
        return data


def normalize(text: str) -> str:
    return " ".join(text.split()).casefold()


def find_evidence(data: bytes, source: Source, phrase: str, *, converted: bool = False) -> dict:
    needle = normalize(phrase)
    if len(needle) < 3 or len(needle) > 500:
        raise ValueError("Search phrase must contain between 3 and 500 normalized characters.")
    try:
        reader = PdfReader(BytesIO(data), strict=False)
        if reader.is_encrypted:
            raise ValueError("Encrypted PDFs cannot be page-verified by this agent.")
        page_count = len(reader.pages)
        if page_count > MAX_PAGES:
            raise ValueError(f"PDF exceeds the {MAX_PAGES}-page processing limit.")
        labels = reader.page_labels
        matches = []
        text_pages = 0
        total_matches = 0
        for number, page in enumerate(reader.pages, start=1):
            text = " ".join((page.extract_text() or "").split())
            if text:
                text_pages += 1
            # Match case-insensitively while retaining offsets into the original text.
            found = list(re.finditer(re.escape(phrase.strip()), text, flags=re.IGNORECASE))
            if not found:
                # Whitespace in the user's phrase may differ from PDF line wrapping.
                pattern = r"\s+".join(re.escape(word) for word in phrase.split())
                found = list(re.finditer(pattern, text, flags=re.IGNORECASE))
            for hit in found:
                total_matches += 1
                if len(matches) < MAX_MATCHES:
                    matches.append({
                        "page_number": number,
                        "page_label": labels[number - 1],
                        "quote": text[max(0, hit.start() - 160):hit.end() + 240],
                        "matched_text": hit.group(),
                    })
    except PdfReadError as exc:
        raise ValueError("PDF could not be parsed; no page citation is available.") from exc
    return {
        "source": source.model_dump(exclude_none=True),
        "sha256": hashlib.sha256(data).hexdigest(),
        "pagination": "converted_pdf" if converted else "original_pdf",
        "page_count": page_count,
        "matches": matches,
        "matches_truncated": total_matches > MAX_MATCHES,
        "text_pages": text_pages,
        "limitation": (
            "No extractable text: this PDF needs OCR; no verified text or page matches are available."
            if text_pages == 0 else
            "Pages without extractable text were not searched; OCR may be needed."
            if text_pages < page_count else None
        ),
    }
