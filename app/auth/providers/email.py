"""Email magic link auth provider for FastAPI."""

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
import duckdb

from app.auth.jwt import create_access_token
from app.auth.dependencies import _get_db
from src.repositories.users import UserRepository

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth/email", tags=["auth"])

MAGIC_LINK_EXPIRY = 3600  # 1 hour


class MagicLinkRequest(BaseModel):
    email: str


class MagicLinkVerify(BaseModel):
    email: str
    token: str


def is_available() -> bool:
    return bool(os.environ.get("SMTP_HOST") or os.environ.get("SENDGRID_API_KEY"))


@router.post("/send-link")
async def send_magic_link(
    request: MagicLinkRequest,
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Send a magic link to the user's email."""
    repo = UserRepository(conn)
    user = repo.get_by_email(request.email)

    # Always return success to prevent email enumeration
    if not user:
        return {"message": "If this email is registered, you will receive a login link."}

    # Generate token
    token = secrets.token_urlsafe(32)
    repo.update(
        id=user["id"],
        reset_token=token,
        reset_token_created=datetime.now(timezone.utc),
    )

    # Send email (best effort)
    try:
        _send_email(request.email, token)
    except Exception as e:
        logger.error(f"Failed to send magic link email: {e}")

    return {"message": "If this email is registered, you will receive a login link."}


@router.post("/verify")
async def verify_magic_link(
    request: MagicLinkVerify,
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Verify a magic link token and issue JWT."""
    repo = UserRepository(conn)
    user = repo.get_by_email(request.email)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid link")

    if user.get("reset_token") != request.token:
        raise HTTPException(status_code=401, detail="Invalid or expired link")

    # Check expiry
    created = user.get("reset_token_created")
    if created:
        if isinstance(created, str):
            created = datetime.fromisoformat(created)
        if (datetime.now(timezone.utc) - created).total_seconds() > MAGIC_LINK_EXPIRY:
            raise HTTPException(status_code=401, detail="Link expired")

    # Clear token (one-time use)
    repo.update(id=user["id"], reset_token=None, reset_token_created=None)

    jwt_token = create_access_token(user["id"], user["email"], user["role"])
    return {"access_token": jwt_token, "token_type": "bearer", "email": user["email"], "role": user["role"]}


def _send_email(email: str, token: str):
    """Send magic link email via SMTP or SendGrid."""
    sendgrid_key = os.environ.get("SENDGRID_API_KEY")
    if sendgrid_key:
        import sendgrid
        from sendgrid.helpers.mail import Mail
        sg = sendgrid.SendGridAPIClient(api_key=sendgrid_key)
        server_url = os.environ.get("SERVER_URL", "http://localhost:8000")
        message = Mail(
            from_email=os.environ.get("EMAIL_FROM_ADDRESS", "noreply@example.com"),
            to_emails=email,
            subject="Login Link",
            html_content=f'<p>Click to login: <a href="{server_url}/auth/email/verify?email={email}&token={token}">Login</a></p>',
        )
        sg.send(message)
        return

    smtp_host = os.environ.get("SMTP_HOST")
    if smtp_host:
        import smtplib
        from email.mime.text import MIMEText
        server_url = os.environ.get("SERVER_URL", "http://localhost:8000")
        msg = MIMEText(f"Login link: {server_url}/auth/email/verify?email={email}&token={token}")
        msg["Subject"] = "Login Link"
        msg["From"] = os.environ.get("SMTP_FROM", "noreply@example.com")
        msg["To"] = email
        with smtplib.SMTP(smtp_host, int(os.environ.get("SMTP_PORT", "587"))) as s:
            if os.environ.get("SMTP_USE_TLS", "true").lower() == "true":
                s.starttls()
            smtp_user = os.environ.get("SMTP_USER")
            if smtp_user:
                s.login(smtp_user, os.environ.get("SMTP_PASSWORD", ""))
            s.send_message(msg)
