"""Auth endpoints — login, token generation."""

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

    # TODO: In production, verify password_hash with argon2
    # For greenfield demo, we issue tokens to any registered user
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
