"""Email magic link auth provider for FastAPI."""

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
import duckdb

from app.auth.jwt import create_access_token
from app.auth.dependencies import _get_db, is_local_dev_mode
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
    # In dev mode the link is rendered to logs + response, so the provider is "available"
    # even without SMTP/SendGrid. Keeps the login UI showing the magic-link option.
    if is_local_dev_mode():
        return True
    return bool(os.environ.get("SMTP_HOST") or os.environ.get("SENDGRID_API_KEY"))


def _has_email_transport() -> bool:
    return bool(os.environ.get("SMTP_HOST") or os.environ.get("SENDGRID_API_KEY"))


def _build_magic_link(email: str, token: str) -> str:
    server_url = os.environ.get("SERVER_URL", "http://localhost:8000")
    return f"{server_url}/auth/email/verify?email={email}&token={token}"


@router.post("/send-link")
async def send_magic_link(
    request: MagicLinkRequest,
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Send a magic link to the user's email.

    When SMTP/SendGrid is not configured, or LOCAL_DEV_MODE=1, the link is
    logged to stderr and returned in the response body so a developer can
    click it without an email transport.
    """
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

    link = _build_magic_link(request.email, token)
    send_error: str | None = None
    if _has_email_transport():
        try:
            _send_email(request.email, token)
        except Exception as e:
            send_error = str(e)
            logger.error("Failed to send magic link email to %s: %s", request.email, e)

    # Dev fallback: expose the link in logs + response so you can click it without SMTP.
    # Scoped strictly to LOCAL_DEV_MODE so test and production behavior are unchanged.
    if is_local_dev_mode():
        logger.warning("=" * 60)
        logger.warning("Magic link for %s (LOCAL_DEV_MODE fallback):", request.email)
        logger.warning("    %s", link)
        logger.warning("=" * 60)
        response: dict = {
            "message": "Magic link generated (LOCAL_DEV_MODE) — click dev_link to log in.",
            "dev_link": link,
        }
        if send_error:
            response["send_error"] = send_error
        return response

    return {"message": "If this email is registered, you will receive a login link."}


def _consume_token(conn: duckdb.DuckDBPyConnection, email: str, token: str) -> dict:
    """Validate & consume a magic-link token. Returns the user dict or raises 401."""
    repo = UserRepository(conn)
    user = repo.get_by_email(email)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid link")

    if user.get("reset_token") != token:
        raise HTTPException(status_code=401, detail="Invalid or expired link")

    created = user.get("reset_token_created")
    if created:
        if isinstance(created, str):
            created = datetime.fromisoformat(created)
        # DuckDB returns TIMESTAMP as offset-naive; we stored it as UTC, so assume UTC.
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - created).total_seconds() > MAGIC_LINK_EXPIRY:
            raise HTTPException(status_code=401, detail="Link expired")

    # Clear token (one-time use)
    repo.update(id=user["id"], reset_token=None, reset_token_created=None)
    return user


@router.post("/verify")
async def verify_magic_link(
    request: MagicLinkVerify,
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Verify a magic link token and issue JWT (JSON API for programmatic clients)."""
    user = _consume_token(conn, request.email, request.token)
    jwt_token = create_access_token(user["id"], user["email"], user["role"])
    return {"access_token": jwt_token, "token_type": "bearer", "email": user["email"], "role": user["role"]}


@router.get("/verify")
async def verify_magic_link_get(
    email: str,
    token: str,
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Click-through variant — verifies token, sets cookie, redirects to /dashboard.

    This is the URL we embed in outgoing emails (and the dev-fallback link), so
    clicking it in a mail client logs the user in without a separate API call.
    """
    user = _consume_token(conn, email, token)
    jwt_token = create_access_token(user["id"], user["email"], user["role"])
    # secure=False when DOMAIN is unset so the cookie is actually sent on plain HTTP (dev).
    use_secure = os.environ.get("DOMAIN", "") != ""
    response = RedirectResponse(url="/dashboard", status_code=302)
    response.set_cookie(
        key="access_token", value=jwt_token,
        httponly=True, max_age=86400, samesite="lax",
        secure=use_secure,
    )
    return response


def _send_email(email: str, token: str):
    """Send magic link email via SMTP or SendGrid."""
    link = _build_magic_link(email, token)
    sendgrid_key = os.environ.get("SENDGRID_API_KEY")
    if sendgrid_key:
        import sendgrid
        from sendgrid.helpers.mail import Mail
        sg = sendgrid.SendGridAPIClient(api_key=sendgrid_key)
        message = Mail(
            from_email=os.environ.get("EMAIL_FROM_ADDRESS", "noreply@example.com"),
            to_emails=email,
            subject="Login Link",
            html_content=f'<p>Click to login: <a href="{link}">Login</a></p>',
        )
        sg.send(message)
        return

    smtp_host = os.environ.get("SMTP_HOST")
    if smtp_host:
        import smtplib
        from email.mime.text import MIMEText
        msg = MIMEText(f"Login link: {link}")
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
