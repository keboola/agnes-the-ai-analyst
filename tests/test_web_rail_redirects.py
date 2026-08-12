"""tests/test_web_rail_redirects.py — standalone browse pages fold into the
Library under the rail chrome; topnav serves them unchanged.

302 (not 308) so a later layout flip is not cached permanently — the same
reasoning as the /dashboard → /chat rail redirect.
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_corporate_memory_redirects_to_library_under_rail(seeded_app, monkeypatch):
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
    c = seeded_app["client"]
    resp = c.get("/corporate-memory", headers=_auth(seeded_app["analyst_token"]), follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/library?section=memory_domain"


def test_corporate_memory_stays_a_page_under_topnav(seeded_app, monkeypatch):
    monkeypatch.delenv("AGNES_UI_LAYOUT", raising=False)
    c = seeded_app["client"]
    resp = c.get("/corporate-memory", headers=_auth(seeded_app["analyst_token"]), follow_redirects=False)
    assert resp.status_code == 200


def test_apps_redirects_to_library_under_rail_when_enabled(seeded_app, monkeypatch):
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
    monkeypatch.setenv("AGNES_DATA_APPS_ENABLED", "1")
    c = seeded_app["client"]
    resp = c.get("/apps", headers=_auth(seeded_app["analyst_token"]), follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/library?section=files"


def test_apps_keeps_empty_state_under_rail_when_disabled(seeded_app, monkeypatch):
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
    monkeypatch.delenv("AGNES_DATA_APPS_ENABLED", raising=False)
    c = seeded_app["client"]
    resp = c.get("/apps", headers=_auth(seeded_app["analyst_token"]), follow_redirects=False)
    assert resp.status_code == 200


def test_apps_stays_a_page_under_topnav(seeded_app, monkeypatch):
    monkeypatch.delenv("AGNES_UI_LAYOUT", raising=False)
    monkeypatch.setenv("AGNES_DATA_APPS_ENABLED", "1")
    c = seeded_app["client"]
    resp = c.get("/apps", headers=_auth(seeded_app["analyst_token"]), follow_redirects=False)
    assert resp.status_code == 200
