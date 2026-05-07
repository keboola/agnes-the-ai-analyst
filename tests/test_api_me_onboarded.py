"""POST /api/me/onboarded — flips users.onboarded TRUE for the calling user.

Self-scoped, idempotent, audit-logged. Body optional pydantic model with
`source` field distinguishing 'agnes_init' (CLI auto-fire) from
'self_acknowledged' (the on-page button for users who already set up
locally before v28 shipped).

See origin: docs/brainstorms/home-page-requirements.md §2 + §6.
"""

from __future__ import annotations

import json
import tempfile
import uuid

import pytest


@pytest.fixture
def fresh_db(monkeypatch):
    """Per-test DATA_DIR + JWT secret so the system DB is fresh."""
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("DATA_DIR", tmp)
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")
        yield tmp


def _make_user_and_session(conn, email: str = "u@example.com"):
    """Create a non-admin user, return (user_id, session_jwt)."""
    from src.repositories.users import UserRepository
    from app.auth.jwt import create_access_token

    uid = str(uuid.uuid4())
    UserRepository(conn).create(id=uid, email=email, name=email.split("@")[0])
    token = create_access_token(user_id=uid, email=email)
    return uid, token


def _client():
    from fastapi.testclient import TestClient
    from app.main import app

    return TestClient(app)


def _onboarded(conn, user_id: str) -> bool:
    row = conn.execute(
        "SELECT onboarded FROM users WHERE id = ?", [user_id]
    ).fetchone()
    return bool(row[0])


def _audit_rows(conn, user_id: str, action: str = "user_onboarded"):
    return conn.execute(
        "SELECT id, action, params FROM audit_log "
        "WHERE user_id = ? AND action = ? ORDER BY timestamp",
        [user_id, action],
    ).fetchall()


def test_post_flips_onboarded_to_true(fresh_db):
    """Authed user with onboarded=FALSE → POST → onboarded=TRUE."""
    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    try:
        uid, sess = _make_user_and_session(conn)
        assert _onboarded(conn, uid) is False
    finally:
        conn.close()
        close_system_db()

    c = _client()
    resp = c.post("/api/me/onboarded", cookies={"access_token": sess})
    assert resp.status_code == 200
    body = resp.json()
    assert body["onboarded"] is True

    conn = get_system_db()
    try:
        assert _onboarded(conn, uid) is True
    finally:
        conn.close()
        close_system_db()


def test_post_default_source_is_agnes_init(fresh_db):
    """Empty body defaults to source='agnes_init' in audit_log."""
    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    try:
        uid, sess = _make_user_and_session(conn)
    finally:
        conn.close()
        close_system_db()

    c = _client()
    resp = c.post("/api/me/onboarded", cookies={"access_token": sess})
    assert resp.status_code == 200

    conn = get_system_db()
    try:
        rows = _audit_rows(conn, uid)
        assert len(rows) == 1
        params = json.loads(rows[0][2]) if isinstance(rows[0][2], str) else rows[0][2]
        assert params.get("source") == "agnes_init"
    finally:
        conn.close()
        close_system_db()


def test_post_self_acknowledged_source(fresh_db):
    """Body {source: 'self_acknowledged'} from /home self-mark button."""
    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    try:
        uid, sess = _make_user_and_session(conn)
    finally:
        conn.close()
        close_system_db()

    c = _client()
    resp = c.post(
        "/api/me/onboarded",
        json={"source": "self_acknowledged"},
        cookies={"access_token": sess},
    )
    assert resp.status_code == 200

    conn = get_system_db()
    try:
        rows = _audit_rows(conn, uid)
        params = json.loads(rows[0][2]) if isinstance(rows[0][2], str) else rows[0][2]
        assert params.get("source") == "self_acknowledged"
    finally:
        conn.close()
        close_system_db()


def test_post_idempotent_already_onboarded(fresh_db):
    """Second POST on already-onboarded user returns 200 + writes second
    audit_log row (cheap visibility on duplicate calls)."""
    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    try:
        uid, sess = _make_user_and_session(conn)
    finally:
        conn.close()
        close_system_db()

    c = _client()
    resp1 = c.post("/api/me/onboarded", cookies={"access_token": sess})
    assert resp1.status_code == 200
    resp2 = c.post("/api/me/onboarded", cookies={"access_token": sess})
    assert resp2.status_code == 200

    conn = get_system_db()
    try:
        rows = _audit_rows(conn, uid)
        assert len(rows) == 2  # both calls logged
        assert _onboarded(conn, uid) is True
    finally:
        conn.close()
        close_system_db()


def test_post_unauthenticated_returns_401(fresh_db):
    c = _client()
    resp = c.post("/api/me/onboarded")
    assert resp.status_code in (401, 403)  # depending on auth dependency shape


def test_post_invalid_source_returns_422(fresh_db):
    """Pydantic rejects unknown source value."""
    from src.db import get_system_db, close_system_db

    conn = get_system_db()
    try:
        _, sess = _make_user_and_session(conn)
    finally:
        conn.close()
        close_system_db()

    c = _client()
    resp = c.post(
        "/api/me/onboarded",
        json={"source": "fabricated"},
        cookies={"access_token": sess},
    )
    assert resp.status_code == 422
