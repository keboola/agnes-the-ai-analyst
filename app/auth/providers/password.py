"""Password auth provider for FastAPI."""

import logging
import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
import duckdb

from app.auth.jwt import create_access_token
from app.auth.dependencies import _get_db
from src.repositories.users import UserRepository

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth/password", tags=["auth"])


class PasswordLoginRequest(BaseModel):
    email: str
    password: str


class PasswordSetupRequest(BaseModel):
    email: str
    token: str
    password: str


def is_available() -> bool:
    return True  # Always available


@router.post("/login")
async def password_login(
    request: PasswordLoginRequest,
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Login with email + password."""
    repo = UserRepository(conn)
    user = repo.get_by_email(request.email)
    if not user or not user.get("password_hash"):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    # Verify password
    try:
        from argon2 import PasswordHasher
        ph = PasswordHasher()
        ph.verify(user["password_hash"], request.password)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid email or password")

    token = create_access_token(user["id"], user["email"], user["role"])
    return {"access_token": token, "token_type": "bearer", "email": user["email"], "role": user["role"]}


@router.post("/setup")
async def password_setup(
    request: PasswordSetupRequest,
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Set initial password using setup token."""
    repo = UserRepository(conn)
    user = repo.get_by_email(request.email)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.get("setup_token") != request.token:
        raise HTTPException(status_code=400, detail="Invalid setup token")

    # Hash and save password
    from argon2 import PasswordHasher
    ph = PasswordHasher()
    hashed = ph.hash(request.password)

    repo.update(id=user["id"], password_hash=hashed, setup_token=None)
    token = create_access_token(user["id"], user["email"], user["role"])
    return {"access_token": token, "token_type": "bearer", "message": "Password set successfully"}
