"""FastAPI auth dependencies — current user, role checking."""

from typing import Optional

import duckdb
from fastapi import Depends, HTTPException, Header, Request, status

from app.auth.jwt import verify_token
from src.db import get_system_db
from src.rbac import Role, ROLE_HIERARCHY
from src.repositories.users import UserRepository


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


async def get_current_user(
    request: Request = None,
    authorization: Optional[str] = Header(None),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
) -> dict:
    """Extract and validate JWT from Authorization header or cookie. Returns user dict."""
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
    """Dependency factory: require user has at least the given role."""
    async def _check(user: dict = Depends(get_current_user)):
        user_role = Role(user.get("role", "viewer"))
        if ROLE_HIERARCHY.get(user_role, 0) < ROLE_HIERARCHY.get(minimum_role, 0):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires role {minimum_role.value} or higher",
            )
        return user
    return _check


async def require_admin(user: dict = Depends(get_current_user)) -> dict:
    """Dependency: require user is an admin. Raises 403 otherwise."""
    if user.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return user


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
