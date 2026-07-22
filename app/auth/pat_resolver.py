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
    "agent_pat_wrong_surface",
]

# Path prefixes an agent PAT (typ="agent_pat") is allowed to authenticate
# against. Everything else — legacy `/api/*`, `/git/`, `/marketplace.zip`,
# and the `/api/v1/agents` *management* verbs (those use session auth) —
# hard-rejects with "agent_pat_wrong_surface". Tuple (not a set) because it
# is consumed via ``str.startswith(prefixes_tuple)``.
_AGENT_PAT_ALLOWED_PREFIXES = ("/api/v1/agents/", "/api/v1/sessions/", "/api/v1/jobs/")

# JWT `typ` values that live in `personal_access_tokens` and must run the
# same DB-backed validity chain (revoked/expired/unknown/hash-mismatch +
# last-used bookkeeping) below.
_PAT_LIKE_TYPES = ("pat", "agent_pat")


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
    conn: Optional[duckdb.DuckDBPyConnection],
    token: str,
    request: Optional[Request] = None,
) -> Tuple[Optional[dict], Optional[ResolutionReason]]:
    """Validate a bearer token and return (user_dict, None) on success.

    On failure returns `(None, reason)` — the reason identifies which check
    failed so callers can map to a specific HTTP 401 detail. Side effects
    (last_used_at update, first-use-from-new-ip audit) are best-effort and
    never block authentication.

    ``conn`` is retained for signature stability — repositories are looked
    up via the factory in ``src.repositories`` (DuckDB or Postgres per
    ``AGNES_DB_URL``), so this argument is ignored.
    """
    if not token:
        return None, "no_token"

    payload = verify_token(token)
    if not payload:
        return None, "invalid_token"

    # Stash the verified payload so `agent_id_from_request` (and Task 9) can
    # read claims off the request without re-verifying the JWT.
    if request is not None:
        try:
            request.state.token_payload = payload
        except Exception:
            pass

    if payload.get("typ") == "agent_pat":
        # Callers that omit `request` (git smart-HTTP in
        # app/marketplace_server/git_router.py, MCP HTTP in
        # app/api/mcp_http.py — neither has a natural `Request` object to
        # pass through their auth path) fall through to path="" here, which
        # never matches `_AGENT_PAT_ALLOWED_PREFIXES` — an agent PAT is
        # fail-closed rejected on those surfaces by design, not by accident.
        path = request.url.path if request is not None else ""
        if not path.startswith(_AGENT_PAT_ALLOWED_PREFIXES):
            return None, "agent_pat_wrong_surface"

    typ = payload.get("typ")
    co_session_id = payload.get("chat_session_id")

    if typ == "co_session" or co_session_id:
        # Route chat-session reads through the repo factory so co-session
        # resolution works on either backend (DuckDB or Postgres). The old
        # path read these tables off the always-DuckDB system connection, so on
        # a PG instance the participant / is_co_session lookups came back empty
        # and every co-session token failed closed.
        from src.repositories import chat_session_participants_repo, chat_session_repo

        if typ == "co_session":
            from src.grant_intersection import compute_grant_intersection
            from app.auth.session_principal import SessionPrincipal

            participants = chat_session_participants_repo().get_session_participants(co_session_id)
            if not participants:
                return None, "invalid_token"  # no live participants -> deny
            emails = [p.user_email for p in participants]
            principal = SessionPrincipal(
                session_id=co_session_id,
                participant_user_ids=[p.user_id for p in participants],
                participant_emails=emails,
                # No conn → compute_grant_intersection resolves through the
                # factory (backend-correct) rather than a raw DuckDB conn.
                intersection=compute_grant_intersection(emails),
            )
            return principal, None
        # Defense-in-depth (SR-3): a plain single-user token that names a
        # co-session must never drive it, regardless of _spawn_runner.
        session = chat_session_repo().get_session(co_session_id)
        if session is not None and bool(session.is_co_session):
            return None, "invalid_token"  # FAIL CLOSED

    from src.repositories import users_repo, access_token_repo

    user = users_repo().get_by_id(payload.get("sub", ""))
    if not user:
        return None, "user_not_found"
    if not bool(user.get("active", True)):
        return None, "deactivated"

    if payload.get("typ") not in _PAT_LIKE_TYPES:
        return user, None

    # PAT / agent PAT: extra DB-backed validation (revoked/expired/unknown/hash).
    # Agent PATs live in the same `personal_access_tokens` table with
    # `agent_id` set, so this chain — including revocation and expiry — is
    # identical for both token kinds.
    tokens_repo = access_token_repo()
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
            from src.repositories import audit_repo

            audit_repo().log(
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


def agent_id_from_request(request: Optional[Request]) -> Optional[str]:
    """agent_id claim of the presented agent PAT, or None for other creds.

    Reads the JWT payload stashed on ``request.state.token_payload`` by
    ``resolve_token_to_user`` — no re-verification. For Task 8/9 callers that
    need to know which agent is bound to the current request.

    Caller contract: only meaningful after ``get_current_user`` (or an
    equivalent that runs ``resolve_token_to_user`` against this same
    ``request``) has already succeeded for the current request. This helper
    performs no verification of its own — it trusts whatever was stashed
    earlier in the request lifecycle and returns ``None`` (never raises) if
    nothing was stashed, e.g. because auth hasn't run yet or failed.
    """
    payload = getattr(request.state, "token_payload", None) if request is not None else None
    return payload.get("agent_id") if payload and payload.get("typ") == "agent_pat" else None
