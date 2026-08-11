"""Reading one file's text — the surface an agent was missing.

`GET /api/collections/{cid}/files/{fid}/preview` has always returned the
extracted text (with a `truncated` flag, and a human `reason` when there is
nothing to show), and the web UI's preview modal uses it. Neither MCP nor
the CLI had any way to reach it, so an agent asked "what is in this file?"
could only guess words and search for them — observed live: six failed
`collections_search` calls, an invented `agnes collections cat`, and a wrong
"I don't have access to your files or collections" conclusion.

This adds the missing surfaces on top of the existing endpoint:
`collection_file_read` on BOTH MCP servers and `agnes collections cat`.
Registering it in only one MCP server is the trap this repo keeps
re-learning (#1236) — the stdio server is the one the in-chat agent talks
to, so the parity assertions below are the point, not a formality.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from cli.commands.collections import collections_app

ROOT = Path(__file__).resolve().parents[1]
HTTP_TOOLS = ROOT / "app" / "api" / "mcp" / "foundation_tools.py"
STDIO_TOOLS = ROOT / "cli" / "mcp" / "server.py"

TOOL = "collection_file_read"
runner = CliRunner()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _collection_with_file(seeded_app, token: str, body: bytes, name: str = "note.md") -> tuple:
    c = seeded_app["client"].post("/api/collections", json={"name": "Readable"}, headers=_auth(token))
    assert c.status_code == 201, c.text
    col = c.json()
    up = seeded_app["client"].post(
        f"/api/collections/{col['id']}/files",
        files={"files": (name, body, "text/markdown")},
        headers=_auth(token),
    )
    assert up.status_code == 201, up.text
    return col["id"], up.json()[0]["file_id"]


class TestBothMcpServersExposeIt:
    """The lesson from #1236: one server is not both servers."""

    def test_tool_is_in_the_foundation_set(self):
        from app.api.mcp.foundation_tools import FOUNDATION_TOOL_NAMES

        assert TOOL in FOUNDATION_TOOL_NAMES

    @pytest.mark.parametrize(("source", "which"), [(HTTP_TOOLS, "HTTP"), (STDIO_TOOLS, "stdio")])
    def test_tool_is_defined(self, source, which):
        text = source.read_text(encoding="utf-8")
        assert re.search(rf"def\s+{TOOL}\s*\(", text), f"{TOOL} missing from the {which} server"

    def test_stdio_server_actually_registers_it(self):
        """Source-level presence is not registration.

        A `def` without the `@tool()` decorator satisfies the check above
        while the in-chat agent — which talks to THIS server — never sees
        the tool. The HTTP transports are covered by
        `test_mcp_tool_parity.py` via `FOUNDATION_TOOL_NAMES`; the stdio
        server is a hand-maintained subset and needs its own runtime check.
        """
        import asyncio

        pytest.importorskip("mcp", reason="mcp package not installed")
        from cli.mcp import server as stdio_server

        names = {t.name for t in asyncio.run(stdio_server.mcp.list_tools())}
        assert TOOL in names, f"{TOOL} is defined but not registered on the stdio server"

    @pytest.mark.parametrize(("source", "which"), [(HTTP_TOOLS, "HTTP"), (STDIO_TOOLS, "stdio")])
    def test_docstring_points_back_at_search_for_long_files(self, source, which):
        """A read tool competes with retrieval; say when NOT to use it."""
        text = source.read_text(encoding="utf-8")
        m = re.search(rf"(?:async\s+)?def\s+{TOOL}\s*\(.*?\)\s*->[^:]*:\s*\"\"\"(.*?)\"\"\"", text, re.S)
        assert m, f"{TOOL} has no docstring in the {which} server"
        doc = m.group(1).lower()
        assert "truncat" in doc, "does not warn that long files are truncated"
        assert "search" in doc, "does not point at search for the many-documents case"


class TestEndpointBehaviourItWraps:
    def test_returns_the_text(self, seeded_app):
        tok = seeded_app["admin_token"]
        cid, fid = _collection_with_file(seeded_app, tok, b"# Title\n\nalpha bravo charlie\n")

        r = seeded_app["client"].get(f"/api/collections/{cid}/files/{fid}/preview", headers=_auth(tok))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["kind"] == "text"
        assert "alpha bravo charlie" in body["text"]
        assert body["truncated"] is False

    def test_long_file_is_truncated_not_refused(self, seeded_app):
        """The context guard is the endpoint's, not the caller's."""
        tok = seeded_app["admin_token"]
        cid, fid = _collection_with_file(seeded_app, tok, ("x " * 30_000).encode())

        body = seeded_app["client"].get(f"/api/collections/{cid}/files/{fid}/preview", headers=_auth(tok)).json()
        assert body["truncated"] is True
        assert len(body["text"]) <= 20_000

    def test_a_foreign_collection_is_not_readable(self, seeded_app):
        tok = seeded_app["admin_token"]
        cid, fid = _collection_with_file(seeded_app, tok, b"secret")

        r = seeded_app["client"].get(
            f"/api/collections/{cid}/files/{fid}/preview",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert r.status_code == 404


class TestInlineMediaStillCarriesItsText:
    """A PDF has extracted text — the read surfaces must get it.

    `preview_file` answers every `_PREVIEW_INLINE_MEDIA` extension (pdf, png,
    jpg, …) with `{kind: "pdf"|"image", raw_url}` and returns BEFORE the
    `_extracted_text()` branch. Correct for the modal, which draws the file
    from `raw_url` — but a CLI or an agent cannot draw anything, and PDFs are
    a primary collection format. Without this, "what is in this PDF?" — the
    exact question the read tool exists for — answered "no text preview is
    available" while the text sat in `corpus_chunks`. Devin Review on #1240.
    """

    def _pdf_row(self, seeded_app, token: str) -> tuple:
        c = seeded_app["client"].post("/api/collections", json={"name": "Docs"}, headers=_auth(token))
        col = c.json()
        up = seeded_app["client"].post(
            f"/api/collections/{col['id']}/files",
            files={"files": ("paper.pdf", b"%PDF-1.4 fake", "application/pdf")},
            headers=_auth(token),
        )
        assert up.status_code == 201, up.text
        return col["id"], up.json()[0]["file_id"]

    def test_pdf_preview_includes_extracted_text(self, seeded_app, monkeypatch):
        tok = seeded_app["admin_token"]
        cid, fid = self._pdf_row(seeded_app, tok)
        monkeypatch.setattr("app.api.collections._extracted_text", lambda _fid: "Quarterly revenue was 4.2M.")

        body = seeded_app["client"].get(f"/api/collections/{cid}/files/{fid}/preview", headers=_auth(tok)).json()

        assert body["kind"] == "pdf", "the modal's contract must not change"
        assert body["raw_url"], "the modal still needs its draw URL"
        assert body["text"] == "Quarterly revenue was 4.2M.", "extracted text not surfaced"

    def test_pdf_without_extracted_text_explains_itself(self, seeded_app, monkeypatch):
        """No text is fine — a silent `text: null` with no reason is not."""
        tok = seeded_app["admin_token"]
        cid, fid = self._pdf_row(seeded_app, tok)
        monkeypatch.setattr("app.api.collections._extracted_text", lambda _fid: "")

        body = seeded_app["client"].get(f"/api/collections/{cid}/files/{fid}/preview", headers=_auth(tok)).json()

        assert body["kind"] == "pdf"
        assert not body.get("text")
        assert body.get("reason"), "a text-less PDF must say why, not return a bare null"


class TestAPdfWhoseBytesAreGoneStillReads:
    """Devin Review on this PR (follow-up): the 404 came before the text.

    A collection file can lose its blob — an ingestion that recorded the row
    and never landed the bytes, or storage cleaned up underneath it — while
    its extracted text stays in `corpus_chunks`. The inline-media branch
    404'd `file_blob_missing` before it ever looked, so the new non-browser
    readers reported a hard error for a file whose answer was available. The
    textual branch has always degraded in exactly this situation.

    The 404 is kept for the case it was written for: a text-less medium with
    no bytes, where the modal would otherwise draw a broken embed. And in the
    degraded case `raw_url` is withheld, so the modal has no URL to break on.
    """

    def _pdf_row_gone(self, seeded_app, token: str, monkeypatch) -> tuple:
        """A PDF row whose blob no longer resolves.

        Simulated by neutralising `_blob_path_or_none` rather than deleting
        the file: that helper is also what `_blob_path_or_404` consults, so
        one patch makes both agree the bytes are gone — which is the real
        shape of the failure (row present, blob unreadable).
        """
        c = seeded_app["client"].post("/api/collections", json={"name": "Gone"}, headers=_auth(token))
        col = c.json()
        up = seeded_app["client"].post(
            f"/api/collections/{col['id']}/files",
            files={"files": ("paper.pdf", b"%PDF-1.4 fake", "application/pdf")},
            headers=_auth(token),
        )
        assert up.status_code == 201, up.text
        monkeypatch.setattr("app.api.collections._blob_path_or_none", lambda _row: None)
        return col["id"], up.json()[0]["file_id"]

    def test_missing_blob_with_text_degrades_instead_of_404(self, seeded_app, monkeypatch):
        tok = seeded_app["admin_token"]
        monkeypatch.setattr("app.api.collections._extracted_text", lambda _fid: "Quarterly revenue was 4.2M.")
        cid, fid = self._pdf_row_gone(seeded_app, tok, monkeypatch)

        r = seeded_app["client"].get(f"/api/collections/{cid}/files/{fid}/preview", headers=_auth(tok))

        assert r.status_code == 200, f"the text was available; a 404 throws it away: {r.text}"
        body = r.json()
        assert body["text"] == "Quarterly revenue was 4.2M."
        assert body["raw_url"] is None, "a URL that would 404 must not be handed to the modal"

    def test_missing_blob_without_text_still_404s(self, seeded_app, monkeypatch):
        """The case the 404 was written for must not regress."""
        tok = seeded_app["admin_token"]
        monkeypatch.setattr("app.api.collections._extracted_text", lambda _fid: "")
        cid, fid = self._pdf_row_gone(seeded_app, tok, monkeypatch)

        r = seeded_app["client"].get(f"/api/collections/{cid}/files/{fid}/preview", headers=_auth(tok))

        assert r.status_code == 404
        assert r.json()["detail"] == "file_blob_missing"


class TestCliCat:
    def test_cat_prints_the_text_of_a_pdf(self):
        """The CLI keys on `text`, not on `kind` — a PDF with text is readable."""
        payload = {
            "kind": "pdf",
            "text": "Quarterly revenue was 4.2M.",
            "truncated": False,
            "filename": "paper.pdf",
        }
        with patch("cli.commands.collections.api_get_json", return_value=payload):
            r = runner.invoke(collections_app, ["cat", "col_1", "cf_1"])
        assert r.exit_code == 0, r.output
        assert "Quarterly revenue was 4.2M." in r.output

    def test_cat_on_a_textless_image_relays_the_reason(self):
        payload = {"kind": "image", "text": None, "reason": "Images carry no extractable text."}
        with patch("cli.commands.collections.api_get_json", return_value=payload):
            r = runner.invoke(collections_app, ["cat", "col_1", "cf_1"])
        assert r.exit_code == 1
        assert "no extractable text" in r.output.lower()

    def test_help_lists_cat(self):
        r = runner.invoke(collections_app, ["--help"])
        assert r.exit_code == 0
        assert "cat" in r.output

    def test_cat_prints_the_text(self):
        payload = {"kind": "text", "text": "alpha bravo", "truncated": False, "filename": "n.md"}
        with patch("cli.commands.collections.api_get_json", return_value=payload):
            r = runner.invoke(collections_app, ["cat", "col_1", "cf_1"])
        assert r.exit_code == 0, r.output
        assert "alpha bravo" in r.output

    def test_cat_says_when_output_was_truncated(self):
        """Silently cutting a document is how a wrong summary gets written."""
        payload = {"kind": "text", "text": "alpha", "truncated": True, "filename": "n.md"}
        with patch("cli.commands.collections.api_get_json", return_value=payload):
            r = runner.invoke(collections_app, ["cat", "col_1", "cf_1"])
        assert r.exit_code == 0, r.output
        assert "truncated" in r.output.lower()

    def test_cat_relays_the_reason_when_there_is_no_text(self):
        payload = {"kind": "none", "reason": "This file hasn't been indexed yet."}
        with patch("cli.commands.collections.api_get_json", return_value=payload):
            r = runner.invoke(collections_app, ["cat", "col_1", "cf_1"])
        assert "hasn't been indexed" in r.output

    def test_cat_json_mode_emits_the_payload(self):
        payload = {"kind": "text", "text": "alpha", "truncated": False}
        with patch("cli.commands.collections.api_get_json", return_value=payload):
            r = runner.invoke(collections_app, ["cat", "col_1", "cf_1", "--json"])
        assert json.loads(r.output)["text"] == "alpha"
