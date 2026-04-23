"""Tests for the split /tokens (own) and /admin/tokens (all) UI.

The two routes render distinct templates:
- /tokens       → my_tokens.html (any signed-in user, own PATs, create modal)
- /admin/tokens → admin_tokens.html (admin-only, all users, stat strip,
                                     owner search, sort-by-owner)

/profile 302-redirects to /tokens for back-compat.
"""

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
        ts = datetime.now(timezone.utc) - timedelta(days=last_used_ago_days)
        conn.execute(
            "UPDATE personal_access_tokens SET last_used_at = ?, last_used_ip = ? WHERE id = ?",
            [ts, last_used_ip, tid],
        )
    if revoked:
        repo.revoke(tid)
    return tid


# ── /tokens — "My tokens" (own PATs) — every signed-in user ────────────────

def test_non_admin_sees_my_tokens_page(fresh_db):
    """Non-admin GET /tokens: personal body, New-token CTA, create modal."""
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
    # Non-admin title + eyebrow
    assert "My tokens" in body
    assert "Your account" in body
    assert "Long-lived tokens for CLI" in body
    # Role-awareness marker stays on the page root
    assert 'data-is-admin="false"' in body
    assert 'data-view="my"' in body
    # New-token CTA + create modal are rendered
    assert 'id="new-token-btn"' in body
    assert 'id="create-modal"' in body
    assert 'id="reveal-banner"' in body
    # Admin-only stat strip is NOT rendered
    assert 'id="tokens-counts"' not in body
    assert 'id="count-active"' not in body
    # Owner search (admin-only) is NOT rendered
    assert 'placeholder="Search by owner email' not in body
    # Admin title must not bleed in
    assert "Access tokens" not in body
    assert "Administration" not in body


def test_admin_sees_my_tokens_on_tokens_path(fresh_db):
    """Admin GET /tokens renders the SAME "My tokens" page as non-admins.

    /tokens is always the personal view — admins use /admin/tokens for the
    org-wide list."""
    from fastapi.testclient import TestClient
    from src.db import get_system_db, close_system_db
    from app.main import app

    conn = get_system_db()
    try:
        _, admin_sess = _make_user_and_session(conn, "admin@t", "admin")
    finally:
        conn.close()
        close_system_db()

    client = TestClient(app)
    resp = client.get(
        "/tokens",
        headers={"Accept": "text/html"},
        cookies={"access_token": admin_sess},
    )
    assert resp.status_code == 200, resp.text
    body = resp.text
    # Personal view markers (same as non-admin)
    assert "My tokens" in body
    assert "Your account" in body
    assert 'id="new-token-btn"' in body
    assert 'id="create-modal"' in body
    assert 'data-is-admin="false"' in body
    # Admin-only UI must NOT show on /tokens, even for an admin
    assert 'id="tokens-counts"' not in body
    assert "Access tokens" not in body  # admin hero title
    assert "Administration" not in body


def test_unauthenticated_redirects_from_tokens_page(fresh_db):
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    resp = client.get(
        "/tokens",
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303, 401), resp.text


# ── /admin/tokens — admin-only list of ALL tokens ──────────────────────────

def test_admin_can_render_admin_tokens_page(fresh_db):
    """Admin GET /admin/tokens: the org-wide list with stat strip + owner
    search + sort-by-owner chip."""
    from fastapi.testclient import TestClient
    from src.db import get_system_db, close_system_db
    from app.main import app

    conn = get_system_db()
    try:
        _, admin_sess = _make_user_and_session(conn, "admin@t", "admin")
    finally:
        conn.close()
        close_system_db()

    client = TestClient(app)
    resp = client.get(
        "/admin/tokens",
        headers={"Accept": "text/html"},
        cookies={"access_token": admin_sess},
    )
    assert resp.status_code == 200, resp.text
    body = resp.text
    # Admin-specific title + eyebrow + subtitle
    assert "Access tokens" in body
    assert "Administration" in body
    assert "incident response and offboarding" in body
    # Role-awareness marker
    assert 'data-is-admin="true"' in body
    assert 'data-view="admin"' in body
    # Filter controls
    assert 'id="flt-status"' in body
    assert 'id="flt-user"' in body
    assert 'id="flt-last-used"' in body
    # Stat strip (admin-only)
    assert 'id="tokens-counts"' in body
    assert 'id="count-active"' in body
    assert 'id="count-expiring"' in body
    # Sort-by-owner chip is only on admin page
    assert 'data-sort-key="user_email"' in body
    # Owner search input
    assert 'placeholder="Search by owner email' in body
    # Revoke hook is in JS template
    assert "data-revoke" in body
    # Admin page must NOT have the "New token" CTA or create modal
    assert 'id="new-token-btn"' not in body
    assert 'id="create-modal"' not in body
    assert 'id="reveal-banner"' not in body
    # Admin page must NOT use the "My tokens" title in its main content.
    # (The shared user-menu in the header shows a "My tokens" link for
    # every signed-in user — scope the check to the page body only.)
    page_start = body.find('class="tokens-page"')
    assert page_start != -1, "admin tokens page body marker not found"
    assert "My tokens" not in body[page_start:]


def test_non_admin_cannot_access_admin_tokens_page(fresh_db):
    """Non-admin GET /admin/tokens: 403 (or redirect) — admin-only route."""
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
        "/admin/tokens",
        headers={"Accept": "text/html"},
        cookies={"access_token": sess},
        follow_redirects=False,
    )
    # require_role(Role.ADMIN) denies with 403 for non-admin
    assert resp.status_code in (302, 401, 403), resp.text


def test_admin_tokens_deeplink_preserves_user_query(fresh_db):
    """/admin/users deep-links with ?user=<email>; page should still render
    and contain the owner search input (JS pre-fills it)."""
    from fastapi.testclient import TestClient
    from src.db import get_system_db, close_system_db
    from app.main import app

    conn = get_system_db()
    try:
        _, admin_sess = _make_user_and_session(conn, "admin@t", "admin")
    finally:
        conn.close()
        close_system_db()

    client = TestClient(app)
    resp = client.get(
        "/admin/tokens?user=alice%40example.com",
        headers={"Accept": "text/html"},
        cookies={"access_token": admin_sess},
    )
    assert resp.status_code == 200, resp.text
    # Owner search input is present; JS reads ?user from window.location.
    assert 'id="flt-user"' in resp.text


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
    """The /tokens create-modal submits POST /auth/tokens (name + expires)."""
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
