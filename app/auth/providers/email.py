"""Email magic link auth provider for FastAPI."""

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
import duckdb

from app.auth.jwt import create_access_token
from app.auth.access import is_user_admin
from app.auth.dependencies import _get_db, is_local_dev_mode
from src.repositories.users import UserRepository


def _role_label(user: dict, conn: duckdb.DuckDBPyConnection) -> str:
    if is_user_admin(user["id"], conn):
        return "admin"
    return user.get("role") or "user"

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
    # URL-encode email: a literal '+' in a query string decodes to space per
    # application/x-www-form-urlencoded, which would break addresses like
    # "user+tag@gmail.com" on the GET /verify side.
    server_url = os.environ.get("SERVER_URL", "http://localhost:8000")
    return f"{server_url}/auth/email/verify?email={quote(email, safe='')}&token={token}"


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
    """Validate & consume a magic-link token atomically. Returns the user dict or raises 401.

    Uses a "compare-and-swap" pattern: instead of setting reset_token to NULL
    directly, we first set it to a unique CONSUMED marker that identifies THIS
    consumption attempt, then verify that OUR marker was written. Two concurrent
    verifies will both try to write their marker, but only one will succeed
    (the WHERE clause checks the original token value); the loser's UPDATE is
    a no-op, and the loser sees the winner's marker and fails.

    DuckDB doesn't expose affected-row count, so the marker is the only way
    to distinguish "I won the race" from "someone else won."
    """
    # Compute the TTL cutoff in Python — DuckDB doesn't support
    # parameterized INTERVAL arithmetic (?, INTERVAL) in all builds.
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=MAGIC_LINK_EXPIRY)

    # Unique marker for this consumption attempt — lets us detect who won
    # the race without relying on DuckDB rowcount (which returns -1).
    consume_id = f"CONSUMED:{secrets.token_hex(16)}"

    # Step 1: Atomic compare-and-swap. Only succeeds if the token still
    # matches the original value and hasn't expired. On success, writes
    # OUR consume_id instead of NULL so we can verify ownership.
    # DuckDB raises TransactionContext Error on concurrent row conflicts —
    # catch and treat as "someone else won the race."
    try:
        conn.execute(
            "UPDATE users SET reset_token = ?, reset_token_created = NULL "
            "WHERE email = ? AND reset_token = ? AND reset_token_created IS NOT NULL "
            "AND reset_token_created >= ?",
            [consume_id, email, token, cutoff],
        )
    except Exception as exc:
        err = str(exc).lower()
        if "conflict" in err or "transaction" in err:
            raise HTTPException(status_code=401, detail="Invalid or expired link")
        raise

    # Step 2: Verify that OUR consume_id was written. If a concurrent
    # request won the race, we'll see THEIR consume_id (or NULL if they
    # already cleared it in step 3) — either way, we fail.
    row = conn.execute(
        "SELECT reset_token FROM users WHERE email = ?",
        [email],
    ).fetchone()
    if not row or row[0] != consume_id:
        raise HTTPException(status_code=401, detail="Invalid or expired link")

    # Step 3: Clear the consumed marker. Safe to do unconditionally —
    # only the winner reaches here, and the marker is transient.
    # If this UPDATE fails (DB error), the marker persists but the user
    # can still request a new magic link — not a lockout.
    try:
        conn.execute(
            "UPDATE users SET reset_token = NULL WHERE email = ? AND reset_token = ?",
            [email, consume_id],
        )
    except Exception:
        logger.warning("Failed to clear CONSUMED marker for %s — marker will persist", email)

    # Fetch the user (token is now cleared, but we need the rest of the fields).
    # CAS already validated token + expiry atomically, so no further checks
    # needed — re-running them now would always fail because reset_token was
    # NULL'd in step 3.
    repo = UserRepository(conn)
    user = repo.get_by_email(email)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid link")
    return user


@router.post("/verify")
async def verify_magic_link(
    request: MagicLinkVerify,
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Verify a magic link token and issue JWT (JSON API for programmatic clients)."""
    user = _consume_token(conn, request.email, request.token)
    role_label = _role_label(user, conn)
    jwt_token = create_access_token(user["id"], user["email"], role_label)
    return {"access_token": jwt_token, "token_type": "bearer", "email": user["email"], "role": role_label}


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
    jwt_token = create_access_token(user["id"], user["email"], _role_label(user, conn))
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
