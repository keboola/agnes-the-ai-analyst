"""Tests for the SCHEDULER_API_TOKEN shared-secret auth path."""

import tempfile

import pytest


@pytest.fixture
def fresh_db(monkeypatch):
    """Isolated DuckDB + JWT secret per test, mirroring tests/test_pat.py."""
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("DATA_DIR", tmp)
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")
        # Clean slate — clear any inherited token from the host shell.
        monkeypatch.delenv("SCHEDULER_API_TOKEN", raising=False)
        # Force pristine state — earlier tests in the same session may have
        # opened the singleton; drop it so the new DATA_DIR takes effect.
        from src.db import close_system_db
        close_system_db()
        yield tmp
        close_system_db()


def test_is_scheduler_token_disabled_when_env_unset(fresh_db, monkeypatch):
    """Empty SCHEDULER_API_TOKEN must disable the auth path entirely.

    A bug here would let any caller authenticate with empty Bearer "" — the
    constant-time compare would also be empty — granting admin to anyone.
    """
    from app.auth.scheduler_token import is_scheduler_token

    monkeypatch.delenv("SCHEDULER_API_TOKEN", raising=False)
    assert is_scheduler_token("") is False
    assert is_scheduler_token("anything") is False


def test_is_scheduler_token_disabled_when_env_too_short(fresh_db, monkeypatch):
    """Operator typo (SCHEDULER_API_TOKEN=todo) must NOT grant admin.

    The minimum length floor exists specifically to prevent a 4-char bearer
    from accidentally matching a 4-char misconfigured secret.
    """
    from app.auth.scheduler_token import is_scheduler_token

    monkeypatch.setenv("SCHEDULER_API_TOKEN", "too-short")
    assert is_scheduler_token("too-short") is False


def test_is_scheduler_token_matches_only_exact_value(fresh_db, monkeypatch):
    from app.auth.scheduler_token import is_scheduler_token

    secret = "x" * 64  # > min length
    monkeypatch.setenv("SCHEDULER_API_TOKEN", secret)
    assert is_scheduler_token(secret) is True
    assert is_scheduler_token(secret + "trailing") is False
    assert is_scheduler_token(secret[:-1]) is False
    assert is_scheduler_token("y" * 64) is False


def test_ensure_scheduler_user_seeds_user_and_admin_membership(fresh_db, monkeypatch):
    """First call seeds; second call is a no-op idempotent re-add."""
    from app.auth.scheduler_token import (
        SCHEDULER_USER_EMAIL,
        ensure_scheduler_user,
    )
    from src.db import SYSTEM_ADMIN_GROUP, get_system_db

    conn = get_system_db()
    try:
        user1 = ensure_scheduler_user(conn)
        assert user1["email"] == SCHEDULER_USER_EMAIL
        # Admin group membership exists.
        admin_group = conn.execute(
            "SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP],
        ).fetchone()
        assert admin_group is not None
        membership = conn.execute(
            "SELECT 1 FROM user_group_members WHERE user_id = ? AND group_id = ?",
            [user1["id"], admin_group[0]],
        ).fetchone()
        assert membership is not None

        # Second call — same id, no duplicate membership row.
        user2 = ensure_scheduler_user(conn)
        assert user2["id"] == user1["id"]
        rows = conn.execute(
            "SELECT COUNT(*) FROM user_group_members WHERE user_id = ? AND group_id = ?",
            [user1["id"], admin_group[0]],
        ).fetchone()
        assert rows[0] == 1
    finally:
        conn.close()


def test_get_scheduler_user_lazy_seeds_when_absent(fresh_db, monkeypatch):
    """First lookup with no prior seed should provision on demand.

    The startup hook in app.main also seeds eagerly, but the scheduler may
    present the token before main.py has finished its lifespan setup on a
    cold boot — get_scheduler_user must close that gap.
    """
    from app.auth.scheduler_token import (
        SCHEDULER_USER_EMAIL,
        get_scheduler_user,
    )
    from src.db import get_system_db
    from src.repositories.users import UserRepository

    conn = get_system_db()
    try:
        # Confirm user does not exist before the call.
        assert UserRepository(conn).get_by_email(SCHEDULER_USER_EMAIL) is None
        user = get_scheduler_user(conn)
        assert user is not None
        assert user["email"] == SCHEDULER_USER_EMAIL
    finally:
        conn.close()


def test_require_session_token_rejects_scheduler_secret(fresh_db, monkeypatch):
    """The shared scheduler secret must NOT pass `require_session_token`.

    /auth/tokens (PAT minting) is gated by `require_session_token`, which
    historically rejected only PATs (JWTs with typ=pat). The scheduler
    secret is opaque so verify_token() returns None and the PAT-claim
    check would silently pass — letting a compromised secret forge
    persistent PATs that survive a rotation. Regression guard for the
    Devin review on PR #127.
    """
    import asyncio
    from unittest.mock import MagicMock

    from fastapi import HTTPException

    from app.auth.dependencies import require_session_token

    secret = "x" * 64
    monkeypatch.setenv("SCHEDULER_API_TOKEN", secret)

    request = MagicMock()
    request.headers = {"authorization": f"Bearer {secret}"}
    request.cookies = {}

    user = {"id": "scheduler-id", "email": "scheduler@system.local"}
    try:
        asyncio.run(require_session_token(request=request, user=user))
    except HTTPException as exc:
        assert exc.status_code == 403
        # Detail should signal "interactive only", flavor doesn't matter.
        assert "interactive" in exc.detail.lower()
    else:
        raise AssertionError("require_session_token must reject scheduler secret")
