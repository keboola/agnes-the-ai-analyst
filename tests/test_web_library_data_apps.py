"""tests/test_web_library_data_apps.py — the Library's Data apps band.

Rail-only by construction (the /library route serves library_legacy.html to
topnav before the sections pipeline runs); these tests pin the rail render.
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _seed_app(slug="revenue-dash", name="Revenue dashboard", owner_id="admin1"):
    from src.db import get_system_db
    from src.repositories.data_apps import DataAppsRepository

    conn = get_system_db()
    try:
        DataAppsRepository(conn).create(
            slug=slug,
            name=name,
            owner_user_id=owner_id,
            description="Streamlit revenue overview",
        )
    finally:
        conn.close()


def test_band_lists_visible_app_under_rail(seeded_app, monkeypatch):
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
    monkeypatch.setenv("AGNES_DATA_APPS_ENABLED", "1")
    _seed_app()
    c = seeded_app["client"]
    resp = c.get("/library", headers=_auth(seeded_app["admin_token"]))
    assert resp.status_code == 200
    body = resp.text
    assert "Data apps" in body
    assert 'href="/apps/detail/revenue-dash"' in body
    assert "Revenue dashboard" in body


def test_band_absent_when_feature_disabled(seeded_app, monkeypatch):
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
    monkeypatch.delenv("AGNES_DATA_APPS_ENABLED", raising=False)
    _seed_app(slug="hidden-app", name="Hidden app")
    c = seeded_app["client"]
    resp = c.get("/library", headers=_auth(seeded_app["admin_token"]))
    assert resp.status_code == 200
    assert 'href="/apps/detail/hidden-app"' not in resp.text


def test_non_owner_without_grant_sees_no_app_row(seeded_app, monkeypatch):
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
    monkeypatch.setenv("AGNES_DATA_APPS_ENABLED", "1")
    _seed_app(slug="private-app", name="Private app", owner_id="admin1")
    c = seeded_app["client"]
    resp = c.get("/library", headers=_auth(seeded_app["analyst_token"]))
    assert resp.status_code == 200
    assert 'href="/apps/detail/private-app"' not in resp.text


def test_soon_badge_gone_from_files_band(seeded_app, monkeypatch):
    """The badge promised data apps would land in Files; with the Data apps
    band real, the promise is kept and the badge retires. The Files band only
    renders when the caller has a file/collection, so seed one — without it
    this test is vacuously green on the unfixed code."""
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
    c = seeded_app["client"]
    r = c.post("/api/collections", json={"name": "Badge probe"}, headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 201, r.text
    resp = c.get("/library", headers=_auth(seeded_app["admin_token"]))
    assert resp.status_code == 200
    body = resp.text
    assert "Badge probe" in body  # the Files band really rendered
    assert "Data apps coming soon" not in body
