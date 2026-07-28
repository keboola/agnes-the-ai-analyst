"""Tests for src.ingest.chunking."""

from __future__ import annotations

from src.ingest.chunking import Chunk, chunk_text
from src.ingest.text_extract import ExtractResult


def test_short_text_single_chunk():
    chunks = chunk_text("a short doc")
    assert len(chunks) == 1
    assert chunks[0].ordinal == 0
    assert chunks[0].text == "a short doc"


def test_empty_text_no_chunks():
    assert chunk_text("") == []
    assert chunk_text("   ") == []


def test_long_text_multiple_ordered_chunks():
    text = "x" * 10000
    chunks = chunk_text(text, target_chars=3200, overlap_chars=400)
    assert len(chunks) > 1
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))
    assert all(len(c.text) <= 3200 for c in chunks)


def test_elements_respect_boundaries():
    res = ExtractResult(
        full_text="ignored",
        elements=[("Intro", "first section text"), ("Body", "second section text")],
    )
    chunks = chunk_text(res)
    assert len(chunks) == 2
    assert chunks[0].section_path == "Intro"
    assert chunks[1].section_path == "Body"
    assert chunks[0].text == "first section text"


def test_large_element_is_windowed():
    res = ExtractResult(full_text="", elements=[("Big", "y" * 8000)])
    chunks = chunk_text(res, target_chars=3200, overlap_chars=400)
    assert len(chunks) > 1
    assert all(c.section_path == "Big" for c in chunks)
    assert isinstance(chunks[0], Chunk)
