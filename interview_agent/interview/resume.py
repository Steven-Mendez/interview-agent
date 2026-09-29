"""Resume ingestion: PDF to markdown, stored in Postgres for all three agents."""

from __future__ import annotations

import io

from markitdown import MarkItDown

# One instance for the process: construction scans converter plugins, and
# convert() itself is stateless per call.
_MARKITDOWN = MarkItDown()


def pdf_to_markdown(data: bytes, filename: str = "resume.pdf") -> str:
    """Convert an uploaded PDF to markdown text.

    CPU-bound, pure-Python (pdfminer/pdfplumber): call it via
    anyio.to_thread.run_sync from async code or it stalls the event loop.
    """
    stream = io.BytesIO(data)
    stream.name = filename  # helps markitdown pick the PDF converter
    result = _MARKITDOWN.convert(stream)
    return result.text_content
