"""Web UI routes for Collections — /library and /library/{slug}."""

from __future__ import annotations

import io

# Glyph path fragments (mirror macros/_catalog_card.html kind_glyph).
_DOC_GLYPH = "M7 4h7l4 4v12H7z"  # single document — a one-file artefact
_LIB_GLYPH = "M9 7h6l4 4v9"  # two overlapping sheets — a collection


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _create(seeded_app, name: str) -> dict:
    r = seeded_app["client"].post("/api/collections", json={"name": name}, headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 201, r.text
    return r.json()


def _upload(seeded_app, cid: str, filename: str, content: bytes, ctype: str):
    return seeded_app["client"].post(
        f"/api/collections/{cid}/files",
        files={"files": (filename, io.BytesIO(content), ctype)},
        headers=_auth(seeded_app["admin_token"]),
    )


def test_library_page_renders_with_collections(seeded_app):
    c = seeded_app["client"]
    _create(seeded_app, "LibraryUI Demo")
    r = c.get("/library", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    assert "Your collections" in r.text
    assert "LibraryUI Demo" in r.text


def test_library_detail_renders_for_admin(seeded_app):
    c = seeded_app["client"]
    col = _create(seeded_app, "DetailUI Demo")
    r = c.get(f"/library/{col['slug']}", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    assert "DetailUI Demo" in r.text
    assert "Ask this collection" in r.text
    assert "Upload files" in r.text


def test_library_detail_404_for_missing(seeded_app):
    r = seeded_app["client"].get("/library/does-not-exist", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 404


def test_library_detail_404_for_non_member(seeded_app):
    c = seeded_app["client"]
    col = _create(seeded_app, "Private UI")
    # analyst1 has no grant — returns 404 (not 403) so existence isn't leaked.
    r = c.get(f"/library/{col['slug']}", headers=_auth(seeded_app["analyst_token"]))
    assert r.status_code == 404


def test_library_lists_only_accessible(seeded_app):
    c = seeded_app["client"]
    _create(seeded_app, "Hidden From Analyst")
    r = c.get("/library", headers=_auth(seeded_app["analyst_token"]))
    assert r.status_code == 200
    # analyst1 has no grants → no collection cards, but the page still renders
    assert "Hidden From Analyst" not in r.text


def test_single_file_artefact_presents_as_file(seeded_app, monkeypatch):
    """One file in an artefact reads AS the file — single-document glyph,
    filename + size in the meta, "File" framing, never "a collection with 1
    file" — but the title is the artefact's NAME (what the caller typed), not
    the filename, so distinct names stay distinct (they previously all
    rendered as the same filename)."""
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
    c = seeded_app["client"]
    col = _create(seeded_app, "Solo Upload")
    _upload(seeded_app, col["id"], "report.pdf", b"%PDF-1.4 x", "application/pdf")

    lst = c.get("/artefacts", headers=_auth(seeded_app["admin_token"])).text
    assert "Solo Upload" in lst  # the typed name is the title
    assert "report.pdf" in lst  # filename is surfaced in the meta
    assert _DOC_GLYPH in lst  # single-document glyph
    assert "1 file" not in lst  # never the "1 file" container framing

    det = c.get(f"/library/{col['slug']}", headers=_auth(seeded_app["admin_token"])).text
    assert "Solo Upload" in det  # the typed name is the hero title
    assert "report.pdf" in det  # filename surfaced in the hero meta
    assert "Ask this file" in det


def test_multi_file_artefact_presents_as_collection(seeded_app, monkeypatch):
    """A second file promotes the artefact to a Collection: the list shows
    ``N files`` + the two-sheet glyph, and the detail page reads as a
    collection again."""
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
    c = seeded_app["client"]
    col = _create(seeded_app, "Grouped Upload")
    _upload(seeded_app, col["id"], "a.txt", b"aaa", "text/plain")
    _upload(seeded_app, col["id"], "b.txt", b"bbb", "text/plain")

    lst = c.get("/artefacts", headers=_auth(seeded_app["admin_token"])).text
    assert "2 files" in lst
    assert _LIB_GLYPH in lst  # two-sheet collection glyph

    det = c.get(f"/library/{col['slug']}", headers=_auth(seeded_app["admin_token"])).text
    assert "Ask this collection" in det


def test_single_file_artefacts_with_same_filename_keep_distinct_names(seeded_app, monkeypatch):
    """Regression: two single-file artefacts holding the *same* filename but
    given different names must render under their own names on the Artefacts
    list — the title is the name, not the filename, so they don't collapse
    into two identical rows."""
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
    c = seeded_app["client"]
    a = _create(seeded_app, "First Report")
    b = _create(seeded_app, "Second Report")
    _upload(seeded_app, a["id"], "logo.png", b"\x89PNG\r\n\x1a\n x", "image/png")
    _upload(seeded_app, b["id"], "logo.png", b"\x89PNG\r\n\x1a\n x", "image/png")

    lst = c.get("/artefacts", headers=_auth(seeded_app["admin_token"])).text
    assert "First Report" in lst
    assert "Second Report" in lst
