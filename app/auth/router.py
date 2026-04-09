"""Auth endpoints — login, token generation, bootstrap."""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import duckdb
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from app.auth.jwt import create_access_token
from app.auth.dependencies import _get_db
from src.repositories.users import UserRepository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


class TokenRequest(BaseModel):
    email: str
    password: str = ""


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: str
    email: str
    role: str


class BootstrapRequest(BaseModel):
    email: str
    name: str = ""
    password: str = ""


def _audit(user_id: str, action: str, result: str | None = None) -> None:
    """Fire-and-forget audit log entry. Swallows all errors."""
    try:
        from src.db import get_system_db
        from src.repositories.audit import AuditRepository
        audit_conn = get_system_db()
        AuditRepository(audit_conn).log(
            user_id=user_id,
            action=action,
            resource="auth",
            result=result,
        )
        audit_conn.close()
    except Exception:
        pass  # Audit failure must not block auth


@router.post("/token", response_model=TokenResponse)
async def create_token(
    request: TokenRequest,
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Issue a JWT token. Requires password authentication."""
    repo = UserRepository(conn)
    user = repo.get_by_email(request.email)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    # If user has password_hash, require and verify it
    if user.get("password_hash"):
        if not request.password:
            raise HTTPException(status_code=401, detail="Password required")
        try:
            ph = PasswordHasher()
            ph.verify(user["password_hash"], request.password)
        except VerifyMismatchError:
            _audit(user["id"], "login_failed", result="invalid_password")
            raise HTTPException(status_code=401, detail="Invalid password")
        except Exception:
            logger.exception("Unexpected error during password verification")
            raise HTTPException(status_code=500, detail="Internal server error")
    else:
        # No password set — must use their auth provider (Google OAuth, magic link)
        raise HTTPException(
            status_code=401,
            detail="This account uses external authentication. Please log in via your configured provider.",
        )

    token = create_access_token(
        user_id=user["id"],
        email=user["email"],
        role=user["role"],
    )
    _audit(user["id"], "token_created")
    return TokenResponse(
        access_token=token,
        user_id=user["id"],
        email=user["email"],
        role=user["role"],
    )


@router.post("/bootstrap", response_model=TokenResponse)
async def bootstrap(
    request: BootstrapRequest,
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Create the first admin user. Only works when no users exist.

    This endpoint allows an AI agent to bootstrap a fresh instance
    without needing docker exec or SSH. It automatically deactivates
    after the first user is created.
    """
    repo = UserRepository(conn)
    existing = repo.list_all()
    if existing:
        raise HTTPException(
            status_code=403,
            detail=f"Bootstrap disabled — {len(existing)} users already exist. Use /auth/token to login.",
        )

    user_id = str(uuid.uuid4())
    password_hash = None
    if request.password:
        password_hash = PasswordHasher().hash(request.password)

    repo.create(
        id=user_id,
        email=request.email,
        name=request.name or request.email.split("@")[0],
        role="admin",
        password_hash=password_hash,
    )

    token = create_access_token(user_id=user_id, email=request.email, role="admin")
    _audit(user_id, "bootstrap_completed")
    return TokenResponse(
        access_token=token,
        user_id=user_id,
        email=request.email,
        role="admin",
    )
