"""Password auth provider for FastAPI."""

import logging
import os

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
import duckdb
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

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
    if not bool(user.get("active", True)):
        raise HTTPException(status_code=401, detail="Account deactivated")

    # Verify password
    try:
        ph = PasswordHasher()
        ph.verify(user["password_hash"], request.password)
    except VerifyMismatchError:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    except Exception:
        logger.exception("Unexpected error during password verification")
        raise HTTPException(status_code=500, detail="Internal server error")

    token = create_access_token(user["id"], user["email"], user["role"])
    return {"access_token": token, "token_type": "bearer", "email": user["email"], "role": user["role"]}


@router.post("/login/web")
async def password_login_web(
    email: str = Form(...),
    password: str = Form(""),
    next: str = Form(""),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Web form login — sets cookie and redirects to `next` (or /dashboard)."""
    repo = UserRepository(conn)
    user = repo.get_by_email(email)
    if not user or not user.get("password_hash"):
        return RedirectResponse(url="/login/password?error=invalid", status_code=302)
    if not bool(user.get("active", True)):
        return RedirectResponse(url="/login/password?error=deactivated", status_code=302)

    try:
        ph = PasswordHasher()
        ph.verify(user["password_hash"], password)
    except (VerifyMismatchError, Exception):
        return RedirectResponse(url="/login/password?error=invalid", status_code=302)

    token = create_access_token(user["id"], user["email"], user["role"])
    # Secure cookie only over HTTPS (detect via X-Forwarded-Proto or request scheme)
    # For dev/staging on plain HTTP, secure=False so the cookie is actually sent
    use_secure = os.environ.get("DOMAIN", "") != ""  # DOMAIN set = production with TLS

    # Sanitize `next`: must start with `/` and must not start with `//` (open-redirect guard)
    target = next if (next.startswith("/") and not next.startswith("//")) else "/dashboard"
    response = RedirectResponse(url=target, status_code=302)
    response.set_cookie(
        key="access_token", value=token,
        httponly=True, max_age=86400, samesite="lax",
        secure=use_secure,
    )
    return response


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
    ph = PasswordHasher()
    hashed = ph.hash(request.password)

    repo.update(id=user["id"], password_hash=hashed, setup_token=None)
    token = create_access_token(user["id"], user["email"], user["role"])
    return {"access_token": token, "token_type": "bearer", "message": "Password set successfully"}
