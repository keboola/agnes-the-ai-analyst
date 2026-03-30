"""Auth endpoints — login, token generation, bootstrap."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import duckdb

from app.auth.jwt import create_access_token
from app.auth.dependencies import _get_db
from src.repositories.users import UserRepository

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


@router.post("/token", response_model=TokenResponse)
async def create_token(
    request: TokenRequest,
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Issue a JWT token. For dev/demo: any registered user gets a token."""
    repo = UserRepository(conn)
    user = repo.get_by_email(request.email)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    # If user has password_hash, verify it
    if user.get("password_hash") and request.password:
        try:
            from argon2 import PasswordHasher
            ph = PasswordHasher()
            ph.verify(user["password_hash"], request.password)
        except Exception:
            raise HTTPException(status_code=401, detail="Invalid password")

    token = create_access_token(
        user_id=user["id"],
        email=user["email"],
        role=user["role"],
    )
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
        try:
            from argon2 import PasswordHasher
            password_hash = PasswordHasher().hash(request.password)
        except ImportError:
            import hashlib
            password_hash = hashlib.sha256(request.password.encode()).hexdigest()

    repo.create(
        id=user_id,
        email=request.email,
        name=request.name or request.email.split("@")[0],
        role="admin",
        password_hash=password_hash,
    )

    token = create_access_token(user_id=user_id, email=request.email, role="admin")
    return TokenResponse(
        access_token=token,
        user_id=user_id,
        email=request.email,
        role="admin",
    )
