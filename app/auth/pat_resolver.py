"""Shared token → user resolution.

Both the JSON API (Bearer header / cookie) and the git smart-HTTP endpoint
(HTTP Basic where the password field carries the PAT) need the same chain:

    verify JWT → user exists & active → if typ=pat: still valid in DB →
    best-effort audit & last-used bookkeeping → return user dict.

Extracted from `app.auth.dependencies.get_current_user` so both paths run
identical checks. Returns `(user, reason)`:

  - on success: `(user_dict, None)`
  - on failure: `(None, reason)` where reason is one of the strings below

The reason lets `get_current_user` map to a specific HTTP 401 detail
(`"Account deactivated"`, `"Token revoked"`, ...) while the WSGI git router
can discard it and just treat any non-None reason as unauthenticated.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Literal, Optional, Tuple

import duckdb
from fastapi import Request

from app.auth.jwt import verify_token
from src.repositories.access_tokens import AccessTokenRepository
from src.repositories.users import UserRepository

logger = logging.getLogger(__name__)

ResolutionReason = Literal[
    "no_token",
    "invalid_token",
    "user_not_found",
    "deactivated",
    "pat_unknown",
    "pat_revoked",
    "pat_expired",
    "pat_mismatch",
]


def _client_ip(request: Optional[Request]) -> Optional[str]:
    """See app/auth/dependencies._client_ip — same trust model (Caddy-fronted)."""
    if request is None:
        return None
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",", 1)[0].strip() or None
    client = getattr(request, "client", None)
    return getattr(client, "host", None) if client else None


def resolve_token_to_user(
    conn: duckdb.DuckDBPyConnection,
    token: str,
    request: Optional[Request] = None,
) -> Tuple[Optional[dict], Optional[ResolutionReason]]:
    """Validate a bearer token and return (user_dict, None) on success.

    On failure returns `(None, reason)` — the reason identifies which check
    failed so callers can map to a specific HTTP 401 detail. Side effects
    (last_used_at update, first-use-from-new-ip audit) are best-effort and
    never block authentication.
    """
    if not token:
        return None, "no_token"

    payload = verify_token(token)
    if not payload:
        return None, "invalid_token"

    user = UserRepository(conn).get_by_id(payload.get("sub", ""))
    if not user:
        return None, "user_not_found"
    if not bool(user.get("active", True)):
        return None, "deactivated"

    if payload.get("typ") != "pat":
        return user, None

    # PAT: extra DB-backed validation (revoked/expired/unknown/hash).
    tokens_repo = AccessTokenRepository(conn)
    record = tokens_repo.get_by_id(payload.get("jti", ""))
    if not record:
        return None, "pat_unknown"
    if record.get("revoked_at") is not None:
        return None, "pat_revoked"

    exp_at = record.get("expires_at")
    if exp_at is not None:
        if isinstance(exp_at, str):
            exp_at = datetime.fromisoformat(exp_at)
        if exp_at.tzinfo is None:
            exp_at = exp_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > exp_at:
            return None, "pat_expired"

    # Defense-in-depth: stored token_hash must match sha256(bearer JWT).
    # Protects against a forged-but-unrevoked JWT using a stolen signing key.
    stored_hash = record.get("token_hash")
    if stored_hash:
        actual = hashlib.sha256(token.encode()).hexdigest()
        if actual != stored_hash:
            return None, "pat_mismatch"

    # First-use-from-new-IP audit entry (#12 acceptance criterion).
    # Only emit when the IP changes on a *subsequent* use — the very
    # first use of a token is not surprising and doesn't need an entry.
    current_ip = _client_ip(request)
    previous_ip = record.get("last_used_ip")
    already_used = record.get("last_used_at") is not None
    if already_used and current_ip and current_ip != previous_ip:
        try:
            from src.repositories.audit import AuditRepository
            AuditRepository(conn).log(
                user_id=user["id"],
                action="token.first_use_new_ip",
                resource=f"token:{payload['jti']}",
                params={"ip": current_ip, "previous_ip": previous_ip},
            )
        except Exception:
            pass  # audit failure must not block auth

    try:
        tokens_repo.mark_used(payload["jti"], ip=current_ip)
    except Exception:
        pass

    return user, None
