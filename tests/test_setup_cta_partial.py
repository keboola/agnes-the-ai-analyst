"""``_claude_setup_cta.jinja`` — shared "Setup a new Claude Code" partial.

Both /dashboard and /home (not-onboarded) include the partial so the
clipboard payload, error reporting, and fallback modal stay in lockstep.
These tests pin that contract: a marker change on one page that doesn't
appear on the other usually means the partial was forked instead of
shared.
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


def _client():
    from fastapi.testclient import TestClient
    from app.main import app

    return TestClient(app)


# Markers from the shared partial that MUST appear on every page that
# wires up the one-click setup flow.
_PARTIAL_MARKERS = (
    "SETUP_INSTRUCTIONS_TEMPLATE",            # JS template array from _claude_setup_instructions.jinja
    "renderSetupInstructions",                 # JS renderer function
    "function setupNewClaude",                 # async fn the button click invokes
    "function showSetupFallback",              # clipboard-blocked modal
    ".setup-fallback-modal",                   # modal CSS
    'fetch(\'/auth/tokens\'',                  # token-mint endpoint
    "__setupCtaWired",                         # IIFE re-include guard
)


def _assert_partial_present(body: str) -> None:
    missing = [m for m in _PARTIAL_MARKERS if m not in body]
    assert not missing, (
        "Shared setup-CTA markers missing from rendered body: %r" % missing
    )


def test_dashboard_includes_setup_cta_partial(fresh_db):
    """Pre-existing dashboard CTA still renders after the JS extraction."""
    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    try:
        _, sess = _make_user_and_session(conn)
    finally:
        conn.close()
        close_system_db()

    c = _client()
    resp = c.get("/dashboard", cookies={"access_token": sess})
    assert resp.status_code == 200
    _assert_partial_present(resp.text)
    # Dashboard's own CTA chrome still here too. Button label was
    # standardised across consumers to the canonical action wording
    # documented in the partial.
    assert 'id="setupClaudeBtn"' in resp.text
    assert "Copy install script to clipboard" in resp.text


def test_home_not_onboarded_includes_setup_cta_partial(fresh_db):
    """The new /home wiring renders the same partial markers as dashboard."""
    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    try:
        _, sess = _make_user_and_session(conn)
        # Default onboarded=FALSE → not-onboarded view.
    finally:
        conn.close()
        close_system_db()

    c = _client()
    resp = c.get("/home", cookies={"access_token": sess})
    assert resp.status_code == 200
    _assert_partial_present(resp.text)
    assert 'id="setupClaudeBtn"' in resp.text
    # Button label was relabeled to read as the action it performs.
    assert "Copy install script to clipboard" in resp.text


def test_home_renders_preview_under_manual_fallback(fresh_db):
    """The collapsed 'Or paste manually' details exposes the same
    `setup-preview-pre` block that /setup uses, so users can read the
    payload without committing to the one-click flow."""
    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    try:
        _, sess = _make_user_and_session(conn)
    finally:
        conn.close()
        close_system_db()

    c = _client()
    resp = c.get("/home", cookies={"access_token": sess})
    assert resp.status_code == 200
    body = resp.text
    assert "manual-fallback" in body
    assert "setup-preview-pre" in body
    assert "placeholder-token" in body  # rendered placeholder span
