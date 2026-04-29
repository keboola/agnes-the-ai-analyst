"""FastAPI auth dependencies — current user resolution.

Authorization helpers (require_admin, require_resource_access) live in
``app.auth.access`` to avoid a circular import — they need ``get_current_user``
from this module and ``_get_db``, which both come from here.
"""

import json
import logging
import os
from typing import Optional

import duckdb
from fastapi import Depends, HTTPException, Header, Request, status

from app.auth.jwt import verify_token
from src.db import get_system_db
from src.repositories.users import UserRepository

logger = logging.getLogger(__name__)

# Default dev user used when LOCAL_DEV_MODE=1. Seeded at startup by app/main.py.
LOCAL_DEV_DEFAULT_EMAIL = "dev@localhost"

# Single-slot cache for the parsed LOCAL_DEV_GROUPS value, keyed by the raw env
# string. Avoids re-parsing JSON on every authenticated request without the
# surprise of test isolation issues — when the env changes (typical in tests),
# the key changes and the cache transparently re-parses.
_LOCAL_DEV_GROUPS_CACHE: tuple[str, list[dict]] | None = None

# Map pat_resolver.ResolutionReason → HTTP 401 `detail` string. Preserves the
# specific user-facing messages that existed before the pat_resolver refactor
# (Account deactivated, Token revoked, ...) so tests and admin UX that grep
# for these phrases keep working.
_AUTH_DETAIL_BY_REASON = {
    "deactivated": "Account deactivated",
    "user_not_found": "User not found",
    "pat_unknown": "Token unknown",
    "pat_revoked": "Token revoked",
    "pat_expired": "Token expired",
    "pat_mismatch": "Token mismatch",
    "invalid_token": "Invalid or expired token",
    "no_token": "Invalid or expired token",
}


def is_local_dev_mode() -> bool:
    """True when LOCAL_DEV_MODE=1 — unsafe for production, bypasses auth."""
    return os.environ.get("LOCAL_DEV_MODE", "").lower() in ("1", "true", "yes")


def get_local_dev_email() -> str:
    """Email of the auto-logged-in dev user. Configurable via LOCAL_DEV_USER_EMAIL."""
    return os.environ.get("LOCAL_DEV_USER_EMAIL", LOCAL_DEV_DEFAULT_EMAIL)


def get_local_dev_groups() -> list[dict]:
    """Mock Google Workspace groups for the dev user when LOCAL_DEV_MODE is on.

    Reads ``LOCAL_DEV_GROUPS`` as a JSON array of objects matching the shape
    produced by ``_fetch_google_groups`` — ``[{"id": "...", "name": "..."}]``.
    Items must have a non-empty ``id``; ``name`` defaults to ``id`` when
    omitted. Extra fields are preserved verbatim so future group attributes
    (roles, labels, …) can be mocked without touching this parser.

    Returns ``[]`` on missing/empty/malformed input — dev mock must never
    break the dev flow. Malformed input is logged at WARNING.

    Cached single-slot: re-parses only when the raw env-var value changes.
    """
    global _LOCAL_DEV_GROUPS_CACHE
    raw = os.environ.get("LOCAL_DEV_GROUPS", "").strip()
    if _LOCAL_DEV_GROUPS_CACHE is not None and _LOCAL_DEV_GROUPS_CACHE[0] == raw:
        return _LOCAL_DEV_GROUPS_CACHE[1]
    result = _parse_local_dev_groups(raw)
    _LOCAL_DEV_GROUPS_CACHE = (raw, result)
    return result


def _parse_local_dev_groups(raw: str) -> list[dict]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("LOCAL_DEV_GROUPS is not valid JSON, ignoring: %s", e)
        return []
    if not isinstance(parsed, list):
        logger.warning(
            "LOCAL_DEV_GROUPS must be a JSON array, got %s — ignoring",
            type(parsed).__name__,
        )
        return []
    out: list[dict] = []
    for item in parsed:
        if not isinstance(item, dict) or not item.get("id"):
            logger.warning(
                "LOCAL_DEV_GROUPS item must be an object with 'id', skipping: %r",
                item,
            )
            continue
        # Don't mutate the parsed input — keeps the parser pure so the cache
        # value stays a fresh list on each rebuild.
        out.append({**item, "name": item.get("name") or item["id"]})
    return out


def _get_db():
    conn = get_system_db()
    try:
        yield conn
    finally:
        conn.close()


def _client_ip(request: Optional[Request]) -> Optional[str]:
    """Return the request's client IP, preferring the first hop of X-Forwarded-For.

    Trust model: this deployment runs behind Caddy (see repo Caddyfile), which
    strips incoming X-Forwarded-For and sets its own. The leftmost hop is
    therefore trustworthy. If the app is ever exposed directly to the internet
    without a proxy, this value becomes client-settable and should only be
    relied on for audit/diagnostics, never access control. Value is stored in
    personal_access_tokens.last_used_ip and audit_log entries — informational
    only, never authorization.
    """
    if request is None:
        return None
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",", 1)[0].strip() or None
    client = getattr(request, "client", None)
    return getattr(client, "host", None) if client else None


def _get_local_dev_user(conn: duckdb.DuckDBPyConnection) -> Optional[dict]:
    """Return the seeded dev user when LOCAL_DEV_MODE is on, else None."""
    repo = UserRepository(conn)
    user = repo.get_by_email(get_local_dev_email())
    if not user:
        logger.error(
            "LOCAL_DEV_MODE is on but dev user %s is not seeded; expected app startup to seed it",
            get_local_dev_email(),
        )
    return user


async def get_current_user(
    request: Request = None,
    authorization: Optional[str] = Header(None),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
) -> dict:
    """Extract and validate JWT from Authorization header or cookie. Returns user dict.

    No role hydration, no session caches — authorization is decided at gate
    time by ``app.auth.access`` which reads ``user_group_members`` directly.
    """
    if is_local_dev_mode():
        user = _get_local_dev_user(conn)
        if user:
            _attach_admin_flag(user, conn)
            return user
        # Fall through to normal auth if seed missing — surfaces the bug
        # instead of hiding it.

    token = None

    # Try Authorization header first
    if authorization and authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ")

    # Fallback to cookie (for web UI after OAuth redirect)
    if not token and request:
        token = request.cookies.get("access_token")

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header",
        )

    # Shared-secret path for the in-cluster scheduler. Checked before
    # pat_resolver because the scheduler token is not a JWT — feeding it to
    # verify_token() would log a spurious decode warning every cron tick.
    # See app/auth/scheduler_token.py for the threat model.
    from app.auth.scheduler_token import get_scheduler_user, is_scheduler_token
    if is_scheduler_token(token):
        scheduler_user = get_scheduler_user(conn)
        if scheduler_user:
            _attach_admin_flag(scheduler_user, conn)
            return scheduler_user
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Scheduler user not provisioned",
        )

    from app.auth.pat_resolver import resolve_token_to_user
    user, reason = resolve_token_to_user(conn, token, request)
    if user:
        _attach_admin_flag(user, conn)
        return user
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=_AUTH_DETAIL_BY_REASON.get(reason, "Invalid or expired token"),
    )


def _attach_admin_flag(user: dict, conn: duckdb.DuckDBPyConnection) -> None:
    """Inject ``user["is_admin"]`` so templates and route handlers can gate
    admin-only UI without touching the legacy ``users.role`` column.

    v13 nulled out ``users.role`` and moved admin authority onto
    ``user_group_members`` (Admin system group). The web header used to
    gate its admin nav on ``session.user.role == 'admin'``, which silently
    became false for every user — so no admin saw any admin menu items
    after the v13 migration. Computing the flag once per request here
    keeps every consumer in sync with ``app.auth.access.is_user_admin``
    (the same call all server-side admin gates use).
    """
    from app.auth.access import is_user_admin
    user_id = user.get("id")
    if user_id:
        try:
            user["is_admin"] = is_user_admin(user_id, conn)
        except Exception:
            user["is_admin"] = False
    else:
        user["is_admin"] = False


async def get_optional_user(
    request: Request = None,
    authorization: Optional[str] = Header(None),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
) -> Optional[dict]:
    """Like get_current_user but returns None instead of 401 if no token."""
    try:
        return await get_current_user(request=request, authorization=authorization, conn=conn)
    except HTTPException:
        return None


async def require_session_token(request: Request, user: dict = Depends(get_current_user)) -> dict:
    """Like get_current_user but rejects PAT — for endpoints that must not
    be callable via a long-lived CI token (e.g. creating new tokens, changing password)."""
    auth = request.headers.get("authorization", "")
    token = None
    if auth.startswith("Bearer "):
        token = auth.removeprefix("Bearer ")
    if not token and request:
        token = request.cookies.get("access_token")
    if token:
        from app.auth.jwt import verify_token
        payload = verify_token(token) or {}
        if payload.get("typ") == "pat":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This endpoint requires an interactive session, not a PAT",
            )
    return user
