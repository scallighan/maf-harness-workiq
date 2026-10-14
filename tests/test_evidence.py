import base64
from io import BytesIO

import pytest
from pydantic import ValidationError
from pypdf import PdfWriter

from workplace_agent.evidence import Blob, Source, find_evidence

SOURCE = Source(
    kind="onedrive", title="Plan.pdf", resource_path="/me/drive/items/123",
    web_url="https://example.sharepoint.com/Plan.pdf", folder="/drive/root:/Planning",
)


def test_pages_quotes_labels_and_conversion(pdf_bytes: bytes) -> None:
    result = find_evidence(pdf_bytes, SOURCE, "RENEWAL   deadline", converted=True)
    assert result["page_count"] == 3
    assert result["pagination"] == "converted_pdf"
    assert result["matches"] == [{
        "page_number": 2, "page_label": "ii",
        "quote": "The renewal deadline is October 30.", "matched_text": "renewal deadline",
    }]
    assert result["source"]["folder"] == "/drive/root:/Planning"
    assert len(result["sha256"]) == 64
    assert result["limitation"] is None


def test_no_match_is_not_evidence(pdf_bytes: bytes) -> None:
    assert find_evidence(pdf_bytes, SOURCE, "invented statement")["matches"] == []


def test_scanned_and_encrypted_pdf() -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    output = BytesIO()
    writer.write(output)
    result = find_evidence(output.getvalue(), SOURCE, "deadline")
    assert result["matches"] == []
    assert "OCR" in result["limitation"]
    writer.encrypt("password")
    output = BytesIO()
    writer.write(output)
    with pytest.raises(ValueError, match="Encrypted"):
        find_evidence(output.getvalue(), SOURCE, "deadline")


@pytest.mark.parametrize("override", [
    {"statusCode": 403}, {"contentType": "application/octet-stream"},
    {"base64Content": "not-base64"}, {"sizeBytes": 1},
])
def test_blob_rejects_invalid_payload(pdf_bytes: bytes, override: dict) -> None:
    values = {
        "statusCode": 200, "contentType": "application/pdf", "sizeBytes": len(pdf_bytes),
        "base64Content": base64.b64encode(pdf_bytes).decode(),
    } | override
    with pytest.raises(ValueError):
        Blob.model_validate(values).pdf_bytes()


def test_source_rejects_unsafe_links() -> None:
    with pytest.raises(ValidationError, match="HTTPS"):
        Source(kind="onedrive", title="Plan.pdf", resource_path="/me/drive/items/1",
               web_url="https://user:password@example.com/file")
