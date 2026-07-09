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


def test_pypdf_is_a_core_dependency(tmp_path):
    """Default installs must extract PDF text without the docling extra.

    Guards the dependency, not pypdf itself: a minimal one-page PDF with a
    text content stream must round-trip through extract_text.
    """
    import pypdf  # noqa: F401  — core dep, not an extra

    # Minimal valid PDF with proper structure for pypdf extraction.
    pdf = (
        b"%PDF-1.4\n"
        b"1 0 obj\n"
        b"<< /Type /Catalog /Pages 2 0 R >>\n"
        b"endobj\n"
        b"2 0 obj\n"
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>\n"
        b"endobj\n"
        b"3 0 obj\n"
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>\n"
        b"endobj\n"
        b"4 0 obj\n"
        b"<< /Length 44 >>\n"
        b"stream\n"
        b"BT /F1 12 Tf 100 700 Td (Hello Agnes) Tj ET\n"
        b"endstream\n"
        b"endobj\n"
        b"5 0 obj\n"
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\n"
        b"endobj\n"
        b"xref\n"
        b"0 6\n"
        b"0000000000 65535 f \n"
        b"0000000009 00000 n \n"
        b"0000000058 00000 n \n"
        b"0000000115 00000 n \n"
        b"0000000262 00000 n \n"
        b"0000000354 00000 n \n"
        b"trailer\n"
        b"<< /Size 6 /Root 1 0 R >>\n"
        b"startxref\n"
        b"451\n"
        b"%%EOF\n"
    )
    path = tmp_path / "hello.pdf"
    path.write_bytes(pdf)

    result = extract_text(str(path), "pdf")
    assert isinstance(result, ExtractResult)
    assert "Hello Agnes" in result.full_text
