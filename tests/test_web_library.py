"""Web UI routes for Collections — /library and /library/{slug}."""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _create(seeded_app, name: str) -> dict:
    r = seeded_app["client"].post("/api/collections", json={"name": name}, headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 201, r.text
    return r.json()


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


def test_library_detail_403_for_non_member(seeded_app):
    c = seeded_app["client"]
    col = _create(seeded_app, "Private UI")
    # analyst1 has no grant on this collection
    r = c.get(f"/library/{col['slug']}", headers=_auth(seeded_app["analyst_token"]))
    assert r.status_code == 403


def test_library_lists_only_accessible(seeded_app):
    c = seeded_app["client"]
    _create(seeded_app, "Hidden From Analyst")
    r = c.get("/library", headers=_auth(seeded_app["analyst_token"]))
    assert r.status_code == 200
    # analyst1 has no grants → no collection cards, but the page still renders
    assert "Hidden From Analyst" not in r.text
