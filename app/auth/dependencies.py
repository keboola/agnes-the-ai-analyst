"""FastAPI auth dependencies — current user, role checking."""

import json
import logging
import os
from typing import Optional

import duckdb
from fastapi import Depends, HTTPException, Header, Request, status

from app.auth.jwt import verify_token
from src.db import get_system_db
from src.rbac import Role, ROLE_HIERARCHY
from src.repositories.users import UserRepository

logger = logging.getLogger(__name__)

# Default dev user used when LOCAL_DEV_MODE=1. Seeded at startup by app/main.py.
LOCAL_DEV_DEFAULT_EMAIL = "dev@localhost"

# Single-slot cache for the parsed LOCAL_DEV_GROUPS value, keyed by the raw env
# string. Avoids re-parsing JSON on every authenticated request without the
# surprise of test isolation issues — when the env changes (typical in tests),
# the key changes and the cache transparently re-parses.
_LOCAL_DEV_GROUPS_CACHE: tuple[str, list[dict]] | None = None


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
    """Extract and validate JWT from Authorization header or cookie. Returns user dict."""
    if is_local_dev_mode():
        user = _get_local_dev_user(conn)
        if user:
            # Mirror the Google OAuth callback (app/auth/providers/google.py:189-194)
            # which writes session.google_groups on every login — including [] on
            # failure — so group-aware code paths see authoritative state. We
            # match that semantics here while skipping the write when nothing
            # would change: same-value updates are a no-op, and the write on
            # PAT/CLI requests with no prior session + no target is also skipped
            # (target → [], existing → None/[], no transition to record).
            if request is not None and hasattr(request, "session"):
                target_groups = get_local_dev_groups()
                current = request.session.get("google_groups")
                groups_changed = False
                if target_groups and current != target_groups:
                    request.session["google_groups"] = target_groups
                    groups_changed = True
                elif not target_groups and current:
                    # Clear stale groups if the operator unsets LOCAL_DEV_GROUPS
                    # mid-session — matches production's "always-write" semantics.
                    request.session["google_groups"] = []
                    groups_changed = True
                # Populate internal_roles whenever it would otherwise be missing
                # — first request after sign-in or any time groups changed. This
                # mirrors the OAuth callback's unconditional write so a dev
                # request never reaches require_internal_role with the key
                # absent. Skipping when role list is already cached + groups
                # didn't change keeps the per-request cost at a session lookup.
                if groups_changed or "internal_roles" not in request.session:
                    try:
                        from app.auth.role_resolver import resolve_internal_roles
                        resolved = resolve_internal_roles(target_groups, conn)
                        request.session["internal_roles"] = resolved
                        logger.info(
                            "dev-bypass resolved %d internal role(s) for %s: %s",
                            len(resolved),
                            user.get("email", "<unknown>"),
                            resolved or "<none>",
                        )
                    except Exception as e:
                        logger.warning(
                            "dev-bypass: resolve_internal_roles failed: %s", e,
                        )
                        request.session["internal_roles"] = []
            return user
        # Fall through to normal auth if seed missing — surfaces the bug instead of hiding it.

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
    payload = verify_token(token)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )

    repo = UserRepository(conn)
    user = repo.get_by_id(payload.get("sub", ""))
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )
    if not bool(user.get("active", True)):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Account deactivated",
        )

    # PAT validation: check it's not revoked / expired / unknown in DB.
    if payload.get("typ") == "pat":
        from datetime import datetime, timezone
        import hashlib
        from src.repositories.access_tokens import AccessTokenRepository

        def _fail(detail: str) -> None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail=detail
            )

        tokens_repo = AccessTokenRepository(conn)
        record = tokens_repo.get_by_id(payload.get("jti", ""))
        if not record:
            _fail("Token unknown")
        if record.get("revoked_at") is not None:
            _fail("Token revoked")
        exp_at = record.get("expires_at")
        if exp_at is not None:
            if isinstance(exp_at, str):
                exp_at = datetime.fromisoformat(exp_at)
            if exp_at.tzinfo is None:
                exp_at = exp_at.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) > exp_at:
                _fail("Token expired")
        # Defense-in-depth: stored token_hash must match sha256(bearer JWT).
        # Protects against a forged-but-unrevoked JWT using a stolen key.
        stored_hash = record.get("token_hash")
        if stored_hash:
            actual = hashlib.sha256(token.encode()).hexdigest()
            if actual != stored_hash:
                _fail("Token mismatch")

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

        # Record last_used_at / last_used_ip synchronously — acceptable cost; can batch later.
        try:
            tokens_repo.mark_used(payload["jti"], ip=current_ip)
        except Exception:
            pass

    return user


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


def require_role(minimum_role: Role):
    """Dependency factory: require user has at least the given role.

    v9 thin wrapper — delegates to ``require_internal_role(f"core.{role}")``.
    The implies hierarchy (core.admin → core.km_admin → core.analyst →
    core.viewer) preserves the legacy "at least this level" semantics
    automatically: a user holding core.admin satisfies require_role(ANALYST)
    because resolve_internal_roles expands implies before the membership
    check. PAT callers route through user_role_grants the same way OAuth
    callers route through session.internal_roles — see role_resolver.py.
    """
    from app.auth.role_resolver import require_internal_role
    return require_internal_role(f"core.{minimum_role.value}")


async def require_admin(
    request: Request,
    user: dict = Depends(get_current_user),
) -> dict:
    """Dependency: require user is an admin. Raises 403 otherwise.

    v9 thin wrapper over ``require_internal_role("core.admin")`` so the
    PAT-aware session-OR-DB resolution pathway applies uniformly. Existing
    callsites use ``Depends(require_admin)`` (no parens) — the function
    keeps that calling convention by accepting the Request + user deps and
    delegating to the inner check. Behavior is identical to v8 for OAuth
    users (admin role from group_mappings); PAT users now succeed when
    they hold a direct core.admin grant in user_role_grants.
    """
    from app.auth.role_resolver import require_internal_role
    check = require_internal_role("core.admin")
    return await check(request=request, user=user)


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
