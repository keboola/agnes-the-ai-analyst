"""Structure-aware chunking for retrieval.

Char-based (≈4 chars/token) to stay dependency-free — no tokenizer. When the
extractor recovered ``elements`` we chunk along their boundaries (never split a
section mid-way unless it alone exceeds the target); otherwise we fall back to
fixed-size windows with overlap over the full text.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from src.ingest.text_extract import ExtractResult

# ~800 tokens * ~4 chars/token; overlap ~100 tokens.
_TARGET_CHARS = 3200
_OVERLAP_CHARS = 400


@dataclass
class Chunk:
    ordinal: int
    text: str
    section_path: Optional[str] = None


def _window(text: str, target: int, overlap: int) -> List[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= target:
        return [text]
    out: List[str] = []
    step = max(1, target - overlap)
    start = 0
    while start < len(text):
        out.append(text[start : start + target])
        start += step
    return out


def chunk_text(
    source: "ExtractResult | str",
    *,
    target_chars: int = _TARGET_CHARS,
    overlap_chars: int = _OVERLAP_CHARS,
) -> List[Chunk]:
    """Chunk an :class:`ExtractResult` (or raw string) into ordered chunks."""
    if isinstance(source, str):
        elements: List[tuple[Optional[str], str]] = []
        full = source
    else:
        elements = source.elements
        full = source.full_text

    chunks: List[Chunk] = []
    ordinal = 0

    if elements:
        for section_path, text in elements:
            for piece in _window(text, target_chars, overlap_chars):
                chunks.append(Chunk(ordinal=ordinal, text=piece, section_path=section_path))
                ordinal += 1
    else:
        for piece in _window(full, target_chars, overlap_chars):
            chunks.append(Chunk(ordinal=ordinal, text=piece))
            ordinal += 1

    return chunks
