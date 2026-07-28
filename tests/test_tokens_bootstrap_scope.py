"""Tests for PAT scope + ttl_seconds fields (clean-analyst-bootstrap spec).

Behaviors covered:
- bootstrap-analyst scope force-clamps TTL to <= 1h regardless of request
- ttl_seconds wins over expires_in_days when both are set
- ttl_seconds beats expires_in_days even when both passed in same request
- expires_in_days remains the fallback when ttl_seconds is omitted
- ttl_seconds upper bound (10y in seconds) rejects with 400
- ttl_seconds <= 0 rejects with 400
- scope defaults to "general" when omitted (strict: claim must be present)
- audit log row for token.create records the scope param
- create_access_token rejects reserved keys in extra_claims

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
    # Strict assertion: the claim MUST be present. The previous
    # `payload.get("scope", "general") == "general"` form would silently
    # pass even if the route stopped passing extra_claims={"scope": ...}
    # to create_access_token entirely — which is exactly the regression
    # we want to catch.
    assert payload["scope"] == "general"


def test_ttl_seconds_wins_when_both_set(web_session):
    """Spec: ttl_seconds wins over expires_in_days when both are present."""
    resp = web_session.post(
        "/auth/tokens",
        json={
            "name": "test",
            "ttl_seconds": 7200,        # 2 hours
            "expires_in_days": 30,      # 30 days — must be ignored
        },
    )
    assert resp.status_code == 201, resp.text
    payload = _decode(resp.json()["token"])
    delta = payload["exp"] - payload["iat"]
    # ttl_seconds (~7200) wins, NOT expires_in_days (~2_592_000)
    assert delta <= 7200 + 5
    assert delta < 30 * 86400


def test_audit_log_includes_scope(web_session, fresh_db):
    """Audit log row for token creation must record the scope param."""
    import json

    from src.db import close_system_db, get_system_db

    resp = web_session.post(
        "/auth/tokens",
        json={"name": "audit-test", "scope": "bootstrap-analyst"},
    )
    assert resp.status_code == 201, resp.text

    # Read the most recent token-creation audit row directly — same pattern
    # as tests/test_pat.py (token.first_use_new_ip audit assertions).
    conn = get_system_db()
    try:
        rows = conn.execute(
            "SELECT params FROM audit_log WHERE action = 'token.create' "
            "ORDER BY timestamp DESC LIMIT 1"
        ).fetchall()
    finally:
        conn.close()
        close_system_db()

    assert rows, "no audit row found for token.create"
    raw_params = rows[0][0]
    params = json.loads(raw_params) if isinstance(raw_params, str) else raw_params
    assert params.get("scope") == "bootstrap-analyst"


def test_create_access_token_rejects_reserved_extra_claims():
    """extra_claims must not override reserved JWT identity claims."""
    import os

    import jwt as pyjwt

    from app.auth.jwt import create_access_token

    # create_access_token reads JWT_SECRET_KEY from env when TESTING=1.
    # Set both so the call works outside the web_session fixture too.
    os.environ.setdefault("TESTING", "1")
    os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")

    token = create_access_token(
        user_id="real-user",
        email="real@example.com",
        extra_claims={
            "sub": "evil-user",          # reserved — must be ignored
            "email": "evil@example.com", # reserved — must be ignored
            "scope": "custom-scope",     # not reserved — must land
        },
    )
    decoded = pyjwt.decode(token, options={"verify_signature": False})
    assert decoded["sub"] == "real-user"
    assert decoded["email"] == "real@example.com"
    assert decoded["scope"] == "custom-scope"
