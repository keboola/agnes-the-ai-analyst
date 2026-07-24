"""Email magic link auth provider for FastAPI."""

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
import duckdb

from app.auth.jwt import create_access_token, SESSION_COOKIE_MAX_AGE_SECONDS
from app.auth.token_hash import hash_token
from app.auth.access import is_user_admin
from app.auth.dependencies import _get_db, is_local_dev_mode
from app.auth.rate_limit import limiter as _rate_limiter


from src.repositories import (
    users_repo,
)


def _role_label(user: dict, conn: duckdb.DuckDBPyConnection) -> str:
    """Display label for the response payload only — `admin` if the user is
    in the Admin system group, otherwise `user`. Authorization at runtime
    checks `is_user_admin` directly; this label is purely cosmetic."""
    return "admin" if is_user_admin(user["id"], conn) else "user"


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
@_rate_limiter.limit("5/minute")
async def send_magic_link(
    request: Request,
    body: MagicLinkRequest,
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Send a magic link to the user's email.

    When SMTP/SendGrid is not configured, or LOCAL_DEV_MODE=1, the link is
    logged to stderr and returned in the response body so a developer can
    click it without an email transport.
    """
    repo = users_repo()
    user = repo.get_by_email(body.email)

    # Always return success to prevent email enumeration
    if not user:
        return {"message": "If this email is registered, you will receive a login link."}

    # Generate token
    token = secrets.token_urlsafe(32)
    repo.update(
        id=user["id"],
        reset_token=hash_token(token),
        reset_token_created=datetime.now(timezone.utc),
    )

    link = _build_magic_link(body.email, token)
    send_error: str | None = None
    if _has_email_transport():
        try:
            _send_email(body.email, token)
        except Exception as e:
            send_error = str(e)
            logger.error("Failed to send magic link email to %s: %s", body.email, e)

    # Dev fallback: expose the link in logs + response so you can click it without SMTP.
    # Scoped strictly to LOCAL_DEV_MODE so test and production behavior are unchanged.
    if is_local_dev_mode():
        logger.warning("=" * 60)
        logger.warning("Magic link for %s (LOCAL_DEV_MODE fallback):", body.email)
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


def _consume_token(email: str, token: str) -> dict:
    """Validate & consume a magic-link token atomically. Returns the user dict or raises 401.

    Compare-and-swap routed through the repository factory so the read/write
    hits the ACTIVE backend (Postgres when configured). The raw CAS that used
    to run on a DuckDB ``_get_db`` connection here read the frozen DuckDB
    system file on PG instances — the token written by ``send_magic_link``
    (factory) lived in PG, so verification never matched and magic-link login
    401'd (#518). ``users_repo().consume_reset_token`` stamps a unique
    CONSUMED marker and returns True iff THIS call won the race.

    The marker is not cleared afterwards: ``reset_token_created`` is NULL'd by
    the CAS so the stale ``CONSUMED:…`` value can never match a real token, and
    the next ``send_magic_link`` overwrites it. (The old step-3 cleanup was
    explicitly best-effort — "not a lockout".)
    """
    # TTL cutoff computed in Python (parameterized INTERVAL arithmetic isn't
    # portable across backends).
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=MAGIC_LINK_EXPIRY)
    # Unique marker for this consumption attempt — the CAS stamps it so the
    # repo can report who won the race without relying on affected-row counts.
    consume_id = f"CONSUMED:{secrets.token_hex(16)}"

    repo = users_repo()
    if not repo.consume_reset_token(email=email, token=hash_token(token), cutoff=cutoff, consume_id=consume_id):
        raise HTTPException(status_code=401, detail="Invalid or expired link")

    user = repo.get_by_email(email)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid link")
    return user


@router.post("/verify")
@_rate_limiter.limit("10/minute")
async def verify_magic_link(
    request: Request,
    body: MagicLinkVerify,
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Verify a magic link token and issue JWT (JSON API for programmatic clients).

    Rate limited 10/min per IP to slow brute-forcing the 32-byte
    ``reset_token`` (the same column doubles as the magic-link token).
    """
    user = _consume_token(body.email, body.token)
    role_label = _role_label(user, conn)
    jwt_token = create_access_token(user["id"], user["email"])
    return {"access_token": jwt_token, "token_type": "bearer", "email": user["email"], "role": role_label}


@router.get("/verify")
@_rate_limiter.limit("10/minute")
async def verify_magic_link_get(
    request: Request,
    email: str,
    token: str,
):
    """Click-through variant — verifies token, sets cookie, redirects to the
    operator-configured home route.

    This is the URL we embed in outgoing emails (and the dev-fallback link), so
    clicking it in a mail client logs the user in without a separate API call.

    Rate limited 10/min per IP for the same reason as the POST variant —
    don't let the click-through path bypass the brute-force throttle.
    """
    user = _consume_token(email, token)
    jwt_token = create_access_token(user["id"], user["email"])
    # Secure whenever served over HTTPS (proxy-aware via request scheme +
    # resolved public origin), not only when DOMAIN is set — see
    # app.auth.public_url.cookie_secure.
    from app.auth.public_url import cookie_secure

    use_secure = cookie_secure(request)
    from app.instance_config import get_home_route

    response = RedirectResponse(url=get_home_route(), status_code=302)
    from app.instance_config import session_cookie_domain

    response.set_cookie(
        key="access_token",
        value=jwt_token,
        httponly=True,
        max_age=SESSION_COOKIE_MAX_AGE_SECONDS,
        samesite="lax",
        secure=use_secure,
        domain=session_cookie_domain(),
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
