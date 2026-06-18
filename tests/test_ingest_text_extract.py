"""Tests for src.ingest.text_extract — dependency-free fallback paths."""

from __future__ import annotations

import pytest

from src.ingest.text_extract import ExtractResult, UnsupportedDocument, extract_text


def _write(tmp_path, name: str, content: str) -> str:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return str(p)


def test_extract_plain_txt(tmp_path):
    path = _write(tmp_path, "a.txt", "hello world\nsecond line")
    res = extract_text(path, "txt")
    assert isinstance(res, ExtractResult)
    assert "hello world" in res.full_text


def test_extract_markdown(tmp_path):
    path = _write(tmp_path, "a.md", "# Title\n\nbody text here")
    res = extract_text(path, "md")
    assert "body text here" in res.full_text


def test_extract_html_strips_tags(tmp_path):
    path = _write(
        tmp_path,
        "a.html",
        "<html><head><style>.x{color:red}</style></head>"
        "<body><p>visible text</p><script>var x=1;</script></body></html>",
    )
    res = extract_text(path, "html")
    assert "visible text" in res.full_text
    assert "color:red" not in res.full_text
    assert "var x" not in res.full_text


def test_extract_no_extension_reads_as_text(tmp_path):
    path = _write(tmp_path, "README", "plain content")
    res = extract_text(path, None)
    assert "plain content" in res.full_text


def test_unextractable_type_raises(tmp_path):
    path = _write(tmp_path, "model.dwg", "binary-ish")
    with pytest.raises(UnsupportedDocument):
        extract_text(path, "dwg")


def test_pdf_without_reader_raises_unsupported(tmp_path, monkeypatch):
    # Force both docling and pypdf unavailable → PDF is unsupported, not a crash.
    import src.ingest.text_extract as te

    monkeypatch.setattr(te, "_try_docling", lambda path: None)
    monkeypatch.setattr(te, "_try_pdf", lambda path: None)
    path = _write(tmp_path, "doc.pdf", "%PDF-1.4 ...")
    with pytest.raises(UnsupportedDocument):
        extract_text(path, "pdf")
