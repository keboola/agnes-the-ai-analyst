"""Tests for the marketplace auth resolver.

Covers:
- Session JWT resolves through DB user lookup -> email.
- PAT (JWT) requires a non-revoked, non-expired DB row.
- Deactivated user is rejected even with a valid signature.
- Email-as-password when MARKETPLACE_ALLOW_EMAIL_AUTH=1.
- Email-as-password rejected when the env flag is unset.
- LOCAL_DEV_MODE bypass returns the dev email.
- Malformed / unknown credentials -> None.
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone

import pytest

from app.api.marketplace import _auth
from app.auth.jwt import create_access_token


def _basic(password: str) -> str:
    raw = f"x:{password}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def test_resolve_from_basic_session_jwt(seeded_admin):
    """Session JWTs (no DB PAT row needed) resolve through the users table."""
    token = create_access_token(
        user_id=seeded_admin["id"], email=seeded_admin["email"], role="admin",
    )
    assert _auth.resolve_email_from_basic(_basic(token)) == "admin@test"


def test_resolve_from_basic_email_fallback_enabled(configured, monkeypatch):
    monkeypatch.setenv("MARKETPLACE_ALLOW_EMAIL_AUTH", "1")
    assert _auth.resolve_email_from_basic(_basic("admin@test")) == "admin@test"


def test_resolve_from_basic_email_fallback_disabled(configured, monkeypatch):
    monkeypatch.delenv("MARKETPLACE_ALLOW_EMAIL_AUTH", raising=False)
    assert _auth.resolve_email_from_basic(_basic("admin@test")) is None


def test_resolve_from_basic_unknown_email_rejected(configured, monkeypatch):
    monkeypatch.setenv("MARKETPLACE_ALLOW_EMAIL_AUTH", "1")
    # Unknown email (not in user_groups.json) -> fail-closed for the git path.
    assert _auth.resolve_email_from_basic(_basic("stranger@test")) is None


def test_resolve_from_basic_missing(configured):
    assert _auth.resolve_email_from_basic(None) is None
    assert _auth.resolve_email_from_basic("") is None


def test_resolve_from_basic_garbage_password(configured, monkeypatch):
    monkeypatch.setenv("MARKETPLACE_ALLOW_EMAIL_AUTH", "1")
    assert _auth.resolve_email_from_basic(_basic("not.a.jwt.nor.email")) is None


def test_resolve_from_basic_local_dev_mode(configured, monkeypatch):
    # No credentials at all + LOCAL_DEV_MODE=1 -> dev email.
    monkeypatch.setenv("LOCAL_DEV_MODE", "1")
    assert _auth.resolve_email_from_basic(None) == "dev@localhost"


def test_resolve_from_basic_invalid_jwt(configured):
    # JWT-shaped but bad signature.
    bogus = "eyJ0eXAiOiJKV1QifQ.eyJlbWFpbCI6ImFAYi5jIn0.badsignature"
    assert _auth.resolve_email_from_basic(_basic(bogus)) is None


def test_resolve_from_basic_jwt_unknown_user(configured):
    """A valid JWT signature for a user that doesn't exist in the users
    table must be rejected — defends against a forged JWT (if the signing
    secret leaks) claiming an arbitrary `sub`.
    """
    token = create_access_token(
        user_id="does-not-exist", email="ghost@test", role="admin",
    )
    assert _auth.resolve_email_from_basic(_basic(token)) is None


def test_resolve_from_basic_jwt_deactivated_user(seeded_admin):
    """A JWT for a deactivated user must be rejected."""
    from src.db import get_system_db
    conn = get_system_db()
    try:
        conn.execute("UPDATE users SET active = FALSE WHERE id = ?", [seeded_admin["id"]])
    finally:
        conn.close()

    token = create_access_token(
        user_id=seeded_admin["id"], email=seeded_admin["email"], role="admin",
    )
    assert _auth.resolve_email_from_basic(_basic(token)) is None


def test_resolve_from_basic_jwt_uses_db_email_not_payload(seeded_admin):
    """Email returned must come from the DB user row, not the JWT payload.

    This matters if the secret ever leaks: the attacker can forge a JWT with
    any email claim, but `sub` must still resolve to a real DB user — and
    that user's real email is what we serve.
    """
    token = create_access_token(
        user_id=seeded_admin["id"], email="attacker@evil.example", role="admin",
    )
    assert _auth.resolve_email_from_basic(_basic(token)) == "admin@test"


def test_resolve_from_basic_pat_revoked(seeded_admin):
    """A revoked PAT must be rejected even though the signature is valid."""
    from src.db import get_system_db
    from src.repositories.access_tokens import AccessTokenRepository

    token = create_access_token(
        user_id=seeded_admin["id"], email=seeded_admin["email"], role="admin",
        typ="pat", token_id="pat-revoked", omit_exp=True,
    )
    conn = get_system_db()
    try:
        AccessTokenRepository(conn).create(
            id="pat-revoked", user_id=seeded_admin["id"], name="test pat",
            token_hash="", prefix="",
        )
        conn.execute(
            "UPDATE personal_access_tokens SET revoked_at = current_timestamp WHERE id = ?",
            ["pat-revoked"],
        )
    finally:
        conn.close()

    assert _auth.resolve_email_from_basic(_basic(token)) is None


def test_resolve_from_basic_pat_expired(seeded_admin):
    """An expired PAT (DB expires_at in the past) must be rejected."""
    from src.db import get_system_db
    from src.repositories.access_tokens import AccessTokenRepository

    token = create_access_token(
        user_id=seeded_admin["id"], email=seeded_admin["email"], role="admin",
        typ="pat", token_id="pat-expired", omit_exp=True,
    )
    expired = datetime.now(timezone.utc) - timedelta(days=1)
    conn = get_system_db()
    try:
        AccessTokenRepository(conn).create(
            id="pat-expired", user_id=seeded_admin["id"], name="test pat",
            token_hash="", prefix="", expires_at=expired,
        )
    finally:
        conn.close()

    assert _auth.resolve_email_from_basic(_basic(token)) is None


def test_resolve_from_basic_pat_unknown_jti(seeded_admin):
    """A PAT-typed JWT with no matching DB row must be rejected.

    This is the core revocation-gap fix: without DB validation, a forged PAT
    signed with the app secret would have let the holder clone for any seeded
    user. Now it's rejected because there's no DB record.
    """
    token = create_access_token(
        user_id=seeded_admin["id"], email=seeded_admin["email"], role="admin",
        typ="pat", token_id="pat-never-issued", omit_exp=True,
    )
    assert _auth.resolve_email_from_basic(_basic(token)) is None


def test_resolve_from_basic_pat_active(seeded_admin):
    """A properly-issued, unrevoked PAT resolves to the user's email."""
    from src.db import get_system_db
    from src.repositories.access_tokens import AccessTokenRepository

    token = create_access_token(
        user_id=seeded_admin["id"], email=seeded_admin["email"], role="admin",
        typ="pat", token_id="pat-live", omit_exp=True,
    )
    conn = get_system_db()
    try:
        AccessTokenRepository(conn).create(
            id="pat-live", user_id=seeded_admin["id"], name="test pat",
            token_hash="", prefix="",
        )
    finally:
        conn.close()

    assert _auth.resolve_email_from_basic(_basic(token)) == "admin@test"
