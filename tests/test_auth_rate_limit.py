"""#45: per-IP rate limiting on auth endpoints.

Each test re-enables the limiter (the autouse conftest fixture disables it
by default for the rest of the suite) and resets bucket state to avoid
order-dependence. Limits live in ``app.auth.providers.*`` and
``app.auth.router`` decorators — adjust here when you bump them.
"""

from __future__ import annotations

import os
import tempfile
import uuid

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def fresh_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("DATA_DIR", tmp)
        from src.db import close_system_db
        close_system_db()
        yield tmp
        close_system_db()


@pytest.fixture
def app_with_ratelimit(monkeypatch, fresh_db):
    """TestClient with the limiter forced on, bucket reset before each call."""
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")
    monkeypatch.setenv("AGNES_AUTH_RATELIMIT_ENABLED", "1")
    from app.auth.rate_limit import limiter
    limiter.enabled = True
    limiter.reset()
    from app.main import app
    return TestClient(app)


def _seed_admin(fresh_db, password: str | None = None):
    """Seed an admin user, optionally with an argon2-hashed password set."""
    from src.db import SYSTEM_ADMIN_GROUP, get_system_db
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.users import UserRepository
    conn = get_system_db()
    try:
        uid = str(uuid.uuid4())
        password_hash = None
        if password:
            from argon2 import PasswordHasher
            password_hash = PasswordHasher().hash(password)
        UserRepository(conn).create(
            id=uid, email="admin@test", name="Admin",
            password_hash=password_hash,
        )
        admin_gid = conn.execute(
            "SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]
        ).fetchone()[0]
        UserGroupMembersRepository(conn).add_member(uid, admin_gid, source="system_seed")
        return uid
    finally:
        conn.close()


def test_password_login_rate_limited_after_10_requests(app_with_ratelimit, fresh_db):
    """11th request inside the per-minute window → 429."""
    _seed_admin(fresh_db, password="correct-horse-battery-staple")
    statuses = []
    for _ in range(11):
        resp = app_with_ratelimit.post(
            "/auth/password/login",
            json={"email": "admin@test", "password": "wrong"},
        )
        statuses.append(resp.status_code)
    # First 10 may be 401 (wrong password); the 11th must be 429 from slowapi.
    assert statuses[:10] == [401] * 10, f"unexpected pre-limit statuses: {statuses[:10]}"
    assert statuses[10] == 429, f"expected 11th request to 429, got {statuses[10]}"


def test_email_send_link_rate_limited_after_5_requests(app_with_ratelimit, fresh_db):
    """6th /send-link inside the per-minute window → 429.

    Covers the email-bombing scenario: a single IP rotating through random
    recipient addresses gets throttled regardless of whether each address
    actually exists (the endpoint always returns success to prevent
    enumeration, so the limit is the only gate)."""
    statuses = []
    for i in range(6):
        resp = app_with_ratelimit.post(
            "/auth/email/send-link",
            json={"email": f"victim-{i}@example.com"},
        )
        statuses.append(resp.status_code)
    assert statuses[:5] == [200] * 5, f"unexpected pre-limit statuses: {statuses[:5]}"
    assert statuses[5] == 429, f"expected 6th request to 429, got {statuses[5]}"


def test_bootstrap_rate_limited_after_3_requests(app_with_ratelimit, fresh_db):
    """4th /auth/bootstrap inside the per-minute window → 429.

    Bootstrap is one-shot in normal use; the tight 3/minute limit exists
    to slow brute-force enumeration of the 'no users with password yet'
    state without breaking legitimate retry-on-typo flows."""
    statuses = []
    for i in range(4):
        resp = app_with_ratelimit.post(
            "/auth/bootstrap",
            json={"email": f"first-admin-{i}@example.com", "password": "x" * 12},
        )
        statuses.append(resp.status_code)
    # First request 200 (bootstrap path), subsequent 403 (already bootstrapped),
    # but the count includes ALL requests — 4th must be 429 regardless of
    # business-logic outcome of requests 2-3.
    assert statuses[3] == 429, (
        f"expected 4th /bootstrap to 429, got {statuses[3]} (full sequence: {statuses})"
    )


def test_rate_limit_disabled_via_env(monkeypatch, fresh_db):
    """``AGNES_AUTH_RATELIMIT_ENABLED=0`` (operator escape hatch) must let
    every request through, no matter how many fire in the same window."""
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")
    from app.auth.rate_limit import limiter
    limiter.enabled = False  # mirrors what the env-var would do at module load
    limiter.reset()
    from app.main import app
    client = TestClient(app)
    statuses = [
        client.post(
            "/auth/email/send-link",
            json={"email": f"x{i}@example.com"},
        ).status_code
        for i in range(20)
    ]
    assert all(s == 200 for s in statuses), f"unexpected throttling: {statuses}"
