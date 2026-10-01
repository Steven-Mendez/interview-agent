"""Resume ingestion: PDF to markdown, stored in Postgres for all three agents."""

from __future__ import annotations

import io

from markitdown import MarkItDown
from pdfminer.pdfdocument import PDFDocument
from pdfminer.pdfpage import PDFPage
from pdfminer.pdfparser import PDFParser

# One instance for the process: construction scans converter plugins, and
# convert() itself is stateless per call.
_MARKITDOWN = MarkItDown()


def validate_pdf(data: bytes) -> None:
    """Validate the document independently of editable extracted text."""
    if b"%PDF-" not in data[:1024]:
        raise ValueError("The uploaded document is not a PDF")
    document = PDFDocument(PDFParser(io.BytesIO(data)))
    if not document.is_extractable:
        raise ValueError("The PDF does not permit text extraction")
    pages = sum(1 for _ in PDFPage.create_pages(document))
    if pages == 0:
        raise ValueError("The PDF has no readable pages")


def pdf_to_markdown(data: bytes, filename: str = "resume.pdf") -> str:
    """Convert an uploaded PDF to markdown text.

    CPU-bound, pure-Python (pdfminer/pdfplumber): call it via
    anyio.to_thread.run_sync from async code or it stalls the event loop.
    """
    stream = io.BytesIO(data)
    stream.name = filename  # helps markitdown pick the PDF converter
    result = _MARKITDOWN.convert(stream)
    return result.text_content
