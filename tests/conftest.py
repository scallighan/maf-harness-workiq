from io import BytesIO

import pytest
from pypdf import PdfWriter
from pypdf.constants import PageLabelStyle
from pypdf.generic import (
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
)


@pytest.fixture
def pdf_bytes() -> bytes:
    writer = PdfWriter()
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    for text in ("Project introduction", "The renewal deadline is October 30.", "Budget appendix"):
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)}),
        })
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    writer.set_page_label(0, 2, style=PageLabelStyle.LOWERCASE_ROMAN)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()
