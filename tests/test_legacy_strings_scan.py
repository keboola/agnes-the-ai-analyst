"""Tests for legacy-string scan in admin CLAUDE.md template endpoint.

The scanner flags admin-saved CLAUDE.md overrides that still reference the
pre-clean-bootstrap CLI surface (`da sync`, `da fetch`, `data/parquet/`,
`da analyst setup`, `da metrics list/show`). The admin UI surfaces the hits
as a yellow banner so operators know to re-author the override; the scanner
itself is informational only — saves with legacy strings are still accepted.
"""

from __future__ import annotations

import tempfile
import uuid

import pytest
from fastapi.testclient import TestClient

from app.api.claude_md import _LEGACY_STRINGS, _scan_legacy_strings


# ---------------------------------------------------------------------------
# Unit tests — pure-function behaviour
# ---------------------------------------------------------------------------


def test_scan_finds_all_known_legacy_strings():
    text = """
    Run `da sync` then `da fetch web_sessions --where ...`.
    Old workspace at data/parquet/ — see `da analyst setup`.
    Use `da metrics list` and `da metrics show <id>`.
    """
    hits = _scan_legacy_strings(text)
    assert "da sync" in hits
    assert "da fetch" in hits
    assert "data/parquet" in hits
    assert "da analyst setup" in hits
    assert "da metrics list" in hits
    assert "da metrics show" in hits


def test_scan_returns_empty_for_clean_text():
    text = "Use `agnes pull` to refresh, `agnes snapshot create` for ad-hoc, `server/parquet/`."
    assert _scan_legacy_strings(text) == []


def test_scan_returns_unique_sorted_hits():
    text = "da sync da sync data/parquet/ data/parquet/foo"
    hits = _scan_legacy_strings(text)
    assert hits == sorted(set(hits))


def test_legacy_strings_constant_shape():
    assert isinstance(_LEGACY_STRINGS, tuple)
    assert all(isinstance(s, str) for s in _LEGACY_STRINGS)
    assert "da sync" in _LEGACY_STRINGS
    assert "data/parquet" in _LEGACY_STRINGS


# ---------------------------------------------------------------------------
# HTTP-level tests — admin GET surfaces detected hits
#
# Lifts the Bearer-session pattern from tests/test_tokens_bootstrap_scope.py
# (Task 1) — Task 20's shared `web_session` cookie fixture isn't built yet,
# but the endpoint surface we're exercising is identical either way.
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("DATA_DIR", tmp)
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")
        yield tmp


@pytest.fixture
def web_session(fresh_db):
    """TestClient authenticated as an admin user via a Bearer session JWT."""
    from app.auth.jwt import create_access_token
    from app.main import app
    from src.db import close_system_db, get_system_db
    from src.repositories.users import UserRepository
    from tests.helpers.auth import grant_admin

    conn = get_system_db()
    try:
        uid = str(uuid.uuid4())
        UserRepository(conn).create(id=uid, email="admin@example.com", name="Admin")
        grant_admin(conn, uid)
        sess_token = create_access_token(user_id=uid, email="admin@example.com")
    finally:
        conn.close()
        close_system_db()

    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {sess_token}"})
    return client


def test_admin_get_template_returns_legacy_strings_when_override_dirty(web_session):
    """Setting an override containing legacy strings populates the field."""
    put = web_session.put(
        "/api/admin/workspace-prompt-template",
        json={"content": "Run `da sync` and check data/parquet/."},
    )
    assert put.status_code == 200, put.text
    resp = web_session.get("/api/admin/workspace-prompt-template")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "da sync" in body["legacy_strings_detected"]
    assert "data/parquet" in body["legacy_strings_detected"]


def test_admin_get_template_returns_empty_when_clean(web_session):
    put = web_session.put(
        "/api/admin/workspace-prompt-template",
        json={"content": "Use `agnes pull` and check `server/parquet/`."},
    )
    assert put.status_code == 200, put.text
    resp = web_session.get("/api/admin/workspace-prompt-template")
    assert resp.status_code == 200, resp.text
    assert resp.json()["legacy_strings_detected"] == []


# ---------------------------------------------------------------------------
# The standalone /admin/workspace-prompt page was superseded by the unified
# /admin/prompts page (#622), which is client-side-driven (no server-rendered
# legacy banner). The legacy-string DETECTION still flows through the
# grandfathered /api/admin/workspace-prompt-template GET (covered above); the
# page now 308-redirects. These guard that contract.
# ---------------------------------------------------------------------------


def test_admin_workspace_prompt_page_redirects(web_session):
    resp = web_session.get("/admin/workspace-prompt", follow_redirects=False)
    assert resp.status_code == 308
    assert resp.headers["location"] == "/admin/prompts"


def test_legacy_strings_still_detected_via_api_after_redirect(web_session):
    """The legacy-string scan survives the page consolidation: the API still
    surfaces hits for a dirty override."""
    web_session.put(
        "/api/admin/workspace-prompt-template",
        json={"content": "Run `da sync` and check data/parquet/."},
    )
    body = web_session.get("/api/admin/workspace-prompt-template").json()
    assert "da sync" in body["legacy_strings_detected"]
    assert "data/parquet" in body["legacy_strings_detected"]
