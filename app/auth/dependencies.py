"""FastAPI auth dependencies — current user, role checking."""

from enum import Enum
from typing import Optional

import duckdb
from fastapi import Depends, HTTPException, Header, status

from app.auth.jwt import verify_token
from src.db import get_system_db
from src.repositories.users import UserRepository


class Role(str, Enum):
    VIEWER = "viewer"
    ANALYST = "analyst"
    ADMIN = "admin"
    KM_ADMIN = "km_admin"


ROLE_HIERARCHY = {
    Role.VIEWER: 0,
    Role.ANALYST: 1,
    Role.KM_ADMIN: 2,
    Role.ADMIN: 3,
}


def _get_db():
    conn = get_system_db()
    try:
        yield conn
    finally:
        conn.close()


async def get_current_user(
    authorization: Optional[str] = Header(None),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
) -> dict:
    """Extract and validate JWT from Authorization header. Returns user dict."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header",
        )
    token = authorization.removeprefix("Bearer ")
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
    authorization: Optional[str] = Header(None),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
) -> Optional[dict]:
    """Like get_current_user but returns None instead of 401 if no token."""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    try:
        return await get_current_user(authorization, conn)
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
