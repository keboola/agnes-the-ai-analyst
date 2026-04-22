"""Tests for the /admin/tokens admin UI + expanded admin list response."""

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
        "/admin/tokens",
        headers={"Accept": "text/html"},
        cookies={"access_token": admin_token},
    )
    assert resp.status_code == 200, resp.text
    # Title is present
    assert "Tokens" in resp.text
    assert "tokens-title" in resp.text
    # Client-side filter controls are in the rendered HTML
    assert 'id="flt-status"' in resp.text
    assert 'id="flt-user"' in resp.text
    assert 'id="flt-last-used"' in resp.text
    # Revoke hook attribute is present on the row template
    assert "data-revoke" in resp.text


def test_non_admin_is_denied_tokens_page(fresh_db):
    """Analyst session should NOT be able to render /admin/tokens.

    `require_role(Role.ADMIN)` raises 403 when the user is authenticated
    but lacks the role. Accept either 302 (redirect to login) or 403.
    """
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
    assert resp.status_code in (302, 303, 401, 403), resp.text


def test_unauthenticated_redirects_from_tokens_page(fresh_db):
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    resp = client.get(
        "/admin/tokens",
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    # No session → HTML flow redirects to /login
    assert resp.status_code in (302, 303, 401), resp.text


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
