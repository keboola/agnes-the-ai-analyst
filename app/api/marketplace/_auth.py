"""Credential resolver shared by the marketplace endpoints.

The FastAPI endpoints (info, zip) already go through `get_current_user` —
this module exists for the WSGI git endpoint, which can't await the async
FastAPI dependency. We mirror `get_current_user`'s checks in sync code:

- Signature verification (via `app.auth.jwt.verify_token`).
- DB user lookup by `sub`; reject if missing or deactivated.
- For PATs (`typ=="pat"`): DB row exists, not revoked, not expired,
  and stored `token_hash` matches sha256(raw token) if present.

Email-as-credential is a temporary fallback gated by
`MARKETPLACE_ALLOW_EMAIL_AUTH=1`. LOCAL_DEV_MODE short-circuits the same
way agnes's FastAPI auth does.
"""
from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timezone

from app.api.marketplace import _git_backend as git_backend
from app.auth.jwt import verify_token

logger = logging.getLogger(__name__)


def allow_email_auth() -> bool:
    """Public: shared by info.py / zip.py to gate the `?email=` fallback."""
    return os.environ.get("MARKETPLACE_ALLOW_EMAIL_AUTH", "").lower() in ("1", "true", "yes")


def _is_local_dev_mode() -> bool:
    return os.environ.get("LOCAL_DEV_MODE", "").lower() in ("1", "true", "yes")


def _local_dev_email() -> str:
    return os.environ.get("LOCAL_DEV_USER_EMAIL", "dev@localhost")


def _looks_like_jwt(s: str) -> bool:
    # A compact JWT has exactly two dots separating three base64url segments.
    # Emails may contain dots too (e.g. user@sub.example.com), so we use dot
    # count + absence-of-`@` as a discriminator. Base64url alphabet excludes
    # `@` so any credential with `@` is an email.
    return s.count(".") == 2 and "@" not in s


def _resolve_from_jwt(token: str) -> str | None:
    """Verify a JWT and return the caller's email, or None if invalid.

    Mirrors the checks in `app.auth.dependencies.get_current_user`:
    signature, user exists + active, and (for PATs) DB-backed revocation /
    expiry / token_hash match. Fails closed on any DB or lookup error —
    we would rather 401 than leak marketplace contents if the DB is down.

    NOTE: we trust `payload["sub"]` and resolve the email from the DB user
    row, not from `payload["email"]`. That defends against a forged JWT
    (if someone ever leaks the signing secret) that claims an arbitrary
    email — the attacker would still need a real `sub` to resolve.
    """
    payload = verify_token(token)
    if not payload:
        return None

    user_id = payload.get("sub")
    if not user_id:
        return None

    try:
        from src.db import get_system_db
        from src.repositories.users import UserRepository

        conn = get_system_db()
    except Exception:
        logger.exception("marketplace auth: system DB unreachable; failing closed")
        return None

    try:
        user = UserRepository(conn).get_by_id(user_id)
        if not user:
            return None
        if not bool(user.get("active", True)):
            return None

        if payload.get("typ") == "pat":
            if not _pat_is_valid(conn, payload, token):
                return None

        return user.get("email")
    except Exception:
        logger.exception("marketplace auth: unexpected error resolving JWT; failing closed")
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _pat_is_valid(conn, payload: dict, raw_token: str) -> bool:
    """Mirror the PAT-specific checks in get_current_user."""
    from src.repositories.access_tokens import AccessTokenRepository

    jti = payload.get("jti", "")
    record = AccessTokenRepository(conn).get_by_id(jti)
    if not record:
        return False
    if record.get("revoked_at") is not None:
        return False

    exp_at = record.get("expires_at")
    if exp_at is not None:
        if isinstance(exp_at, str):
            exp_at = datetime.fromisoformat(exp_at)
        if exp_at.tzinfo is None:
            exp_at = exp_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > exp_at:
            return False

    stored_hash = record.get("token_hash")
    if stored_hash:
        actual = hashlib.sha256(raw_token.encode()).hexdigest()
        if actual != stored_hash:
            return False

    return True


def resolve_email_from_credential(credential: str | None) -> str | None:
    """Given a raw credential string, return the caller's email or None.

    Detection:
    - Looks like a JWT (two dots, no @) -> verify signature + DB state.
    - Contains @ and fallback enabled -> passthrough after is_known_email.
    - Otherwise -> None.
    """
    if not credential:
        return None

    if _looks_like_jwt(credential):
        return _resolve_from_jwt(credential)

    if "@" in credential:
        if not allow_email_auth():
            return None
        if not git_backend.is_known_email(credential):
            return None
        return credential

    return None


def resolve_email_from_basic(auth_header: str | None) -> str | None:
    """WSGI entrypoint: parse HTTP Basic, resolve password -> email.

    LOCAL_DEV_MODE short-circuits everything and returns the dev email even
    with no credentials, matching agnes's FastAPI dependency behavior.
    """
    if _is_local_dev_mode():
        return _local_dev_email()
    password = git_backend.email_from_basic_auth(auth_header)
    return resolve_email_from_credential(password)
