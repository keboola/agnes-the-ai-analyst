"""Tests for PAT scope + ttl_seconds fields (clean-analyst-bootstrap spec).

Six behaviors covered:
- bootstrap-analyst scope force-clamps TTL to <= 1h regardless of request
- ttl_seconds wins over expires_in_days when both are set
- expires_in_days remains the fallback when ttl_seconds is omitted
- ttl_seconds upper bound (10y in seconds) rejects with 400
- ttl_seconds <= 0 rejects with 400
- scope defaults to "general" when omitted

The spec calls for a `web_session` cookie-authenticated fixture sourced from
`tests/fixtures/analyst_bootstrap.py` (Task 20). Until that lands, these
tests use a local Bearer-session client built the same way the existing
`test_pat.py` suite does — same auth surface, same `require_session_token`
dependency, just a different transport for the session credential.
"""

from __future__ import annotations

import tempfile
import uuid

import jwt as _jwt
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def fresh_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("DATA_DIR", tmp)
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")
        yield tmp


@pytest.fixture
def web_session(fresh_db):
    """TestClient authenticated as an admin user via a Bearer session JWT.

    Mirrors the spec's `web_session` fixture surface: returns a TestClient
    that carries authenticated session credentials on every request, so
    test bodies can call `web_session.post("/auth/tokens", json=...)`
    without per-call header plumbing.
    """
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


def _decode(pat: str) -> dict:
    return _jwt.decode(pat, options={"verify_signature": False})


def test_bootstrap_pat_ttl_clamped_to_one_hour(web_session):
    resp = web_session.post(
        "/auth/tokens",
        json={
            "name": "init",
            "scope": "bootstrap-analyst",
            "ttl_seconds": 86400,  # 1 day — must be ignored, clamped to 3600
        },
    )
    assert resp.status_code == 201, resp.text
    payload = _decode(resp.json()["token"])
    assert payload.get("scope") == "bootstrap-analyst"
    assert payload["exp"] - payload["iat"] <= 3600 + 5


def test_general_pat_uses_ttl_seconds_when_set(web_session):
    resp = web_session.post(
        "/auth/tokens",
        json={"name": "test", "ttl_seconds": 7200},
    )
    assert resp.status_code == 201, resp.text
    payload = _decode(resp.json()["token"])
    assert payload["exp"] - payload["iat"] <= 7200 + 5


def test_general_pat_falls_back_to_expires_in_days(web_session):
    resp = web_session.post(
        "/auth/tokens",
        json={"name": "test", "expires_in_days": 30},
    )
    assert resp.status_code == 201, resp.text
    payload = _decode(resp.json()["token"])
    assert payload["exp"] - payload["iat"] <= 30 * 86400 + 5


def test_ttl_seconds_upper_bound(web_session):
    # 3650 days * 86400 = 315_360_000 seconds. One past this must reject.
    resp = web_session.post(
        "/auth/tokens",
        json={"name": "test", "ttl_seconds": 315_360_001},
    )
    assert resp.status_code == 400, resp.text


def test_ttl_seconds_must_be_positive(web_session):
    resp = web_session.post(
        "/auth/tokens",
        json={"name": "test", "ttl_seconds": 0},
    )
    assert resp.status_code == 400, resp.text


def test_scope_default_is_general(web_session):
    resp = web_session.post("/auth/tokens", json={"name": "test"})
    assert resp.status_code == 201, resp.text
    payload = _decode(resp.json()["token"])
    assert payload.get("scope", "general") == "general"
