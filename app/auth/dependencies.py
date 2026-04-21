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
        # Record last_used_at synchronously — acceptable cost; can batch later.
        try:
            tokens_repo.mark_used(payload["jti"])
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
