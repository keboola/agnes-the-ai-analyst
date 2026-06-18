"""Text extraction for prose documents.

Docling is an OPTIONAL extra (``agnes[docling]``) — it pulls heavy ML deps, so
it is never imported at module top. When importable it gives richer element
structure (and tables); otherwise a lightweight per-format fallback handles the
common text formats with no extra dependencies. Formats that need a parser we
don't have raise :class:`UnsupportedDocument` so the caller can mark the file
``rejected`` rather than indexing garbage.
"""

from __future__ import annotations

import html.parser
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

# Plain-text formats handled by a direct read (no dependency).
_PLAIN_EXTS = {"txt", "md", "markdown", "rtf", "text", "log"}
_HTML_EXTS = {"html", "htm"}


@dataclass
class ExtractResult:
    """Extracted document text.

    ``elements`` is an optional list of ``(section_path, text)`` pairs when the
    extractor recovers structure (e.g. Docling); ``full_text`` is always set.
    """

    full_text: str
    elements: List[Tuple[Optional[str], str]] = field(default_factory=list)


class UnsupportedDocument(Exception):
    """Raised when no available extractor can read the document."""


def _ext_of(path: str, file_type: Optional[str]) -> str:
    if file_type and "/" not in file_type:
        return file_type.lower().lstrip(".")
    if "." in path:
        return path.rsplit(".", 1)[-1].lower()
    return ""


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


class _HTMLTextExtractor(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: List[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip and data.strip():
            self._parts.append(data)

    def text(self) -> str:
        return re.sub(r"\n{3,}", "\n\n", "\n".join(self._parts)).strip()


def _strip_html(raw: str) -> str:
    parser = _HTMLTextExtractor()
    parser.feed(raw)
    return parser.text()


def _try_docling(path: str) -> Optional[ExtractResult]:
    """Use Docling if installed. Returns None when the extra is absent."""
    try:
        from docling.document_converter import DocumentConverter  # type: ignore
    except Exception:
        return None
    try:
        result = DocumentConverter().convert(path)
        md = result.document.export_to_markdown()
        return ExtractResult(full_text=md, elements=[(None, md)])
    except Exception:
        # Docling is present but failed on this doc — fall through to the
        # lightweight path rather than crashing ingestion.
        return None


def _try_pdf(path: str) -> Optional[str]:
    """Extract PDF text with pypdf if importable; None when unavailable."""
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception:
        return None
    try:
        reader = PdfReader(path)
        return "\n\n".join((page.extract_text() or "") for page in reader.pages).strip()
    except Exception:
        return None


def extract_text(path: str, file_type: Optional[str] = None) -> ExtractResult:
    """Extract text from a prose document.

    Order: Docling (if installed) → per-format lightweight fallback. Raises
    :class:`UnsupportedDocument` when nothing can read the file.
    """
    ext = _ext_of(path, file_type)

    doc = _try_docling(path)
    if doc is not None and doc.full_text.strip():
        return doc

    if ext in _PLAIN_EXTS or ext == "":
        return ExtractResult(full_text=_read_text(path))
    if ext in _HTML_EXTS:
        return ExtractResult(full_text=_strip_html(_read_text(path)))
    if ext == "pdf":
        text = _try_pdf(path)
        if text is not None and text.strip():
            return ExtractResult(full_text=text)
        raise UnsupportedDocument("PDF text extraction needs the 'docling' extra or pypdf; neither is available")

    raise UnsupportedDocument(f"no text extractor for '.{ext}'")
