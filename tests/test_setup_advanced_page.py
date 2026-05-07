"""GET /setup-advanced — deeper reference page for the second hour onward.

Splits the rich CoS-guide content (VS Code layout, plugin recommendations,
multi-model second opinions, skills/rules/hooks, project workflows) out of
/home so /home stays scannable. Auth-gated to any authenticated user;
section anchors so /home and other pages can deep-link.
"""

from __future__ import annotations

import tempfile
import uuid

import pytest


@pytest.fixture
def fresh_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("DATA_DIR", tmp)
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")
        yield tmp


def _make_user_and_session(conn, email="u@example.com"):
    from src.repositories.users import UserRepository
    from app.auth.jwt import create_access_token

    uid = str(uuid.uuid4())
    UserRepository(conn).create(id=uid, email=email, name=email.split("@")[0])
    return uid, create_access_token(user_id=uid, email=email)


def _client(follow_redirects: bool = True):
    from fastapi.testclient import TestClient
    from app.main import app

    return TestClient(app, follow_redirects=follow_redirects)


def test_setup_advanced_unauth_redirects_to_login(fresh_db):
    """HTML route → 401-redirect-to-/login per app.main's StarletteHTTPException
    handler. Same contract as /home."""
    c = _client(follow_redirects=False)
    resp = c.get("/setup-advanced")
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("/login")


def test_setup_advanced_authed_renders(fresh_db):
    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    try:
        _, sess = _make_user_and_session(conn)
    finally:
        conn.close()
        close_system_db()

    c = _client()
    resp = c.get("/setup-advanced", cookies={"access_token": sess})
    assert resp.status_code == 200
    body = resp.text
    # Hero + TOC chrome
    assert "advanced-mock" in body
    assert "On this page" in body
    # All eight section anchors
    for anchor in (
        'id="vscode"', 'id="workspace"', 'id="projects"', 'id="plugins"',
        'id="second-opinions"', 'id="skills-rules-hooks"', 'id="first-task"',
        'id="tips"', 'id="yolo"',
    ):
        assert anchor in body, f"missing anchor: {anchor}"


def test_setup_advanced_includes_plugin_table(fresh_db):
    """Plugin recommendations split into Essential / Recommended / Optional
    tiers with the canonical entries from the CoS guide."""
    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    try:
        _, sess = _make_user_and_session(conn)
    finally:
        conn.close()
        close_system_db()

    c = _client()
    body = c.get("/setup-advanced", cookies={"access_token": sess}).text
    # Essential tier
    assert "superpowers" in body
    assert "context7" in body
    assert "github" in body
    # Recommended tier
    assert "playwright" in body
    assert "atlassian" in body
    # Operator-curated marketplace pointer
    assert 'href="/store"' in body


def test_setup_advanced_includes_multi_model_review(fresh_db):
    """Codex + Gemini install prompts present so users can wire up
    second-opinion workflow without leaving the page."""
    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    try:
        _, sess = _make_user_and_session(conn)
    finally:
        conn.close()
        close_system_db()

    c = _client()
    body = c.get("/setup-advanced", cookies={"access_token": sess}).text
    assert "@openai/codex" in body
    assert "@google/gemini-cli" in body
    assert "review-panel" in body
    assert "second-opinion" in body
