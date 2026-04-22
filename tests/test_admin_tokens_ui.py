"""Tests for the unified /tokens UI (role-aware) + expanded admin list response.

The UI was unified in feat/unify-tokens-fullwidth: a single /tokens page
renders a different body depending on the viewer's role. /admin/tokens
now 302-redirects to /tokens for back-compat. Tests exercise /tokens
directly (cookies don't survive TestClient's redirect chain in some
Starlette versions) and add a dedicated assertion for the redirect."""

import hashlib
import tempfile
import uuid
from datetime import datetime, timezone, timedelta

import pytest


@pytest.fixture
def fresh_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("DATA_DIR", tmp)
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")
        yield tmp


def _make_user_and_session(conn, email: str, role: str):
    """Create a user and return (uid, session_jwt)."""
    from src.repositories.users import UserRepository
    from app.auth.jwt import create_access_token

    uid = str(uuid.uuid4())
    UserRepository(conn).create(id=uid, email=email, name=email.split("@")[0], role=role)
    token = create_access_token(user_id=uid, email=email, role=role)
    return uid, token


def _make_pat_row(conn, user_id: str, name: str = "ci",
                  expires_in_days: int = 30, revoked: bool = False,
                  last_used_ip: str | None = None,
                  last_used_ago_days: int | None = None):
    from src.repositories.access_tokens import AccessTokenRepository
    repo = AccessTokenRepository(conn)
    tid = str(uuid.uuid4())
    raw = "r" * 40
    exp = datetime.now(timezone.utc) + timedelta(days=expires_in_days) if expires_in_days is not None else None
    repo.create(
        id=tid, user_id=user_id, name=name,
        token_hash=hashlib.sha256(raw.encode()).hexdigest(),
        prefix=tid.replace("-", "")[:8],
        expires_at=exp,
    )
    if last_used_ago_days is not None:
        # Write a fixed timestamp in the past + ip; go around mark_used so the
        # timestamp is controllable.
        ts = datetime.now(timezone.utc) - timedelta(days=last_used_ago_days)
        conn.execute(
            "UPDATE personal_access_tokens SET last_used_at = ?, last_used_ip = ? WHERE id = ?",
            [ts, last_used_ip, tid],
        )
    if revoked:
        repo.revoke(tid)
    return tid


# ── Page rendering ─────────────────────────────────────────────────────────

def test_admin_can_render_tokens_page(fresh_db):
    """Admin GET /tokens: full admin body with owner search, stat strip, filters."""
    from fastapi.testclient import TestClient
    from src.db import get_system_db, close_system_db
    from app.main import app

    conn = get_system_db()
    try:
        _, admin_token = _make_user_and_session(conn, "admin@t", "admin")
    finally:
        conn.close()
        close_system_db()

    client = TestClient(app)
    resp = client.get(
        "/tokens",
        headers={"Accept": "text/html"},
        cookies={"access_token": admin_token},
    )
    assert resp.status_code == 200, resp.text
    body = resp.text
    # Admin-specific title + eyebrow
    assert "Access tokens" in body
    assert "Administration" in body
    assert "tokens-title" in body
    # Role-awareness marker on the page root
    assert 'data-is-admin="true"' in body
    # Client-side filter controls are in the rendered HTML
    assert 'id="flt-status"' in body
    assert 'id="flt-user"' in body
    assert 'id="flt-last-used"' in body
    # Admin-only stat strip is rendered
    assert 'id="tokens-counts"' in body
    assert 'id="count-active"' in body
    # Revoke hook attribute is present in the page JS template
    assert "data-revoke" in body
    # Admin must NOT see the non-admin New-token CTA / create-modal
    assert 'id="create-modal"' not in body
    assert 'id="reveal-banner"' not in body


def test_non_admin_can_render_tokens_page(fresh_db):
    """Non-admin GET /tokens: personal body with New-token CTA + create modal.

    Previous behavior on /admin/tokens denied non-admins with 403; the
    unified /tokens page serves every signed-in user, with role-gated
    rendering on the template + API side."""
    from fastapi.testclient import TestClient
    from src.db import get_system_db, close_system_db
    from app.main import app

    conn = get_system_db()
    try:
        _, sess = _make_user_and_session(conn, "user@t", "analyst")
    finally:
        conn.close()
        close_system_db()

    client = TestClient(app)
    resp = client.get(
        "/tokens",
        headers={"Accept": "text/html"},
        cookies={"access_token": sess},
    )
    assert resp.status_code == 200, resp.text
    body = resp.text
    # Non-admin-specific title
    assert "My tokens" in body
    assert "Long-lived tokens for CLI" in body
    # Role-awareness marker on the page root
    assert 'data-is-admin="false"' in body
    # New-token CTA + create modal are rendered only for non-admins
    assert 'id="new-token-btn"' in body
    assert 'id="create-modal"' in body
    assert 'id="reveal-banner"' in body
    # Admin-only stat strip is hidden
    assert 'id="tokens-counts"' not in body
    assert 'id="count-active"' not in body
    # Owner search (admin-only) is hidden; the id remains as type=hidden for JS compat
    assert 'placeholder="Search by owner email' not in body


def test_unauthenticated_redirects_from_tokens_page(fresh_db):
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    resp = client.get(
        "/tokens",
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    # No session → HTML flow redirects to /login
    assert resp.status_code in (302, 303, 401), resp.text


# ── Back-compat redirects ─────────────────────────────────────────────────

def test_admin_tokens_redirects_to_tokens(fresh_db):
    """/admin/tokens is kept as a 302 redirect to /tokens, preserving query string."""
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    resp = client.get("/admin/tokens", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/tokens"

    # Deep-link from /admin/users must preserve ?user=foo
    resp = client.get("/admin/tokens?user=alice%40example.com", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("/tokens?user=alice")


def test_profile_redirects_to_tokens(fresh_db):
    """/profile no longer renders — it 302-redirects to /tokens."""
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    resp = client.get("/profile", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/tokens"


# ── Admin list API — expanded fields ───────────────────────────────────────

def test_admin_list_includes_user_email_and_last_used_ip(fresh_db):
    from fastapi.testclient import TestClient
    from src.db import get_system_db, close_system_db
    from app.main import app

    conn = get_system_db()
    try:
        admin_uid, admin_sess = _make_user_and_session(conn, "admin@t", "admin")
        other_uid, _ = _make_user_and_session(conn, "victim@t", "analyst")
        _make_pat_row(conn, other_uid, name="laptop", last_used_ip="9.9.9.9",
                      last_used_ago_days=2)
    finally:
        conn.close()
        close_system_db()

    client = TestClient(app)
    resp = client.get(
        "/auth/admin/tokens",
        headers={"Authorization": f"Bearer {admin_sess}"},
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()
    assert len(items) >= 1
    row = [r for r in items if r["name"] == "laptop"][0]
    assert row["user_id"] == other_uid
    assert row["user_email"] == "victim@t"
    assert row["last_used_ip"] == "9.9.9.9"
    assert row["last_used_at"]  # not None


def test_non_admin_cannot_list_admin_tokens(fresh_db):
    from fastapi.testclient import TestClient
    from src.db import get_system_db, close_system_db
    from app.main import app

    conn = get_system_db()
    try:
        _, analyst_sess = _make_user_and_session(conn, "u@t", "analyst")
    finally:
        conn.close()
        close_system_db()

    client = TestClient(app)
    resp = client.get(
        "/auth/admin/tokens",
        headers={"Authorization": f"Bearer {analyst_sess}"},
    )
    assert resp.status_code == 403


# ── Admin revoke ──────────────────────────────────────────────────────────

def test_admin_can_revoke_another_users_token(fresh_db):
    from fastapi.testclient import TestClient
    from src.db import get_system_db, close_system_db
    from src.repositories.access_tokens import AccessTokenRepository
    from app.main import app

    conn = get_system_db()
    try:
        admin_uid, admin_sess = _make_user_and_session(conn, "admin@t", "admin")
        other_uid, _ = _make_user_and_session(conn, "victim@t", "analyst")
        tid = _make_pat_row(conn, other_uid, name="to-kill")
    finally:
        conn.close()
        close_system_db()

    client = TestClient(app)
    resp = client.delete(
        f"/auth/admin/tokens/{tid}",
        headers={"Authorization": f"Bearer {admin_sess}"},
    )
    assert resp.status_code == 204

    conn = get_system_db()
    try:
        row = AccessTokenRepository(conn).get_by_id(tid)
        assert row is not None
        assert row["revoked_at"] is not None
    finally:
        conn.close()
        close_system_db()


def test_non_admin_can_create_pat_via_tokens_page_api(fresh_db):
    """The non-admin /tokens flow calls POST /auth/tokens with name + expires.

    This mirrors exactly what the create-modal in tokens.html submits."""
    from fastapi.testclient import TestClient
    from src.db import get_system_db, close_system_db
    from src.repositories.access_tokens import AccessTokenRepository
    from app.main import app

    conn = get_system_db()
    try:
        uid, sess = _make_user_and_session(conn, "user@t", "analyst")
    finally:
        conn.close()
        close_system_db()

    client = TestClient(app)
    resp = client.post(
        "/auth/tokens",
        headers={"Authorization": f"Bearer {sess}"},
        json={"name": "laptop", "expires_in_days": 30},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["name"] == "laptop"
    assert data["token"]  # raw JWT returned exactly once
    assert data["prefix"]

    # It must be owned by the creator
    conn = get_system_db()
    try:
        row = AccessTokenRepository(conn).get_by_id(data["id"])
    finally:
        conn.close()
        close_system_db()
    assert row is not None
    assert row["user_id"] == uid
    assert row["name"] == "laptop"


def test_non_admin_cannot_admin_revoke(fresh_db):
    from fastapi.testclient import TestClient
    from src.db import get_system_db, close_system_db
    from app.main import app

    conn = get_system_db()
    try:
        _, analyst_sess = _make_user_and_session(conn, "u@t", "analyst")
        other_uid, _ = _make_user_and_session(conn, "other@t", "analyst")
        tid = _make_pat_row(conn, other_uid, name="keep")
    finally:
        conn.close()
        close_system_db()

    client = TestClient(app)
    resp = client.delete(
        f"/auth/admin/tokens/{tid}",
        headers={"Authorization": f"Bearer {analyst_sess}"},
    )
    assert resp.status_code == 403
