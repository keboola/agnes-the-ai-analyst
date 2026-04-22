"""FastAPI auth dependencies — current user, role checking."""

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


def is_local_dev_mode() -> bool:
    """True when LOCAL_DEV_MODE=1 — unsafe for production, bypasses auth."""
    return os.environ.get("LOCAL_DEV_MODE", "").lower() in ("1", "true", "yes")


def get_local_dev_email() -> str:
    """Email of the auto-logged-in dev user. Configurable via LOCAL_DEV_USER_EMAIL."""
    return os.environ.get("LOCAL_DEV_USER_EMAIL", LOCAL_DEV_DEFAULT_EMAIL)


def _get_db():
    conn = get_system_db()
    try:
        yield conn
    finally:
        conn.close()


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
