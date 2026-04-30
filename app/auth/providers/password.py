"""Password auth provider for FastAPI."""

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
import duckdb
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from app.auth.jwt import create_access_token
from app.auth.access import is_user_admin
from app.auth.dependencies import _get_db, is_local_dev_mode
from src.repositories.users import UserRepository


def _role_label(user: dict, conn: duckdb.DuckDBPyConnection) -> str:
    """Display label for the response payload only — `admin` for Admin
    group members, `user` otherwise. Authorization at runtime checks
    `is_user_admin` directly; this label is purely cosmetic for the
    response shape."""
    return "admin" if is_user_admin(user["id"], conn) else "user"

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth/password", tags=["auth"])

RESET_TOKEN_TTL = timedelta(hours=24)
SETUP_TOKEN_TTL = timedelta(days=7)
MIN_PASSWORD_LEN = 8


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


class PasswordLoginRequest(BaseModel):
    email: str
    password: str


class PasswordSetupRequest(BaseModel):
    email: str
    token: str
    password: str


def is_available() -> bool:
    return True  # Always available


def _has_email_transport() -> bool:
    return bool(os.environ.get("SMTP_HOST") or os.environ.get("SENDGRID_API_KEY"))


def _cookie_secure() -> bool:
    # Secure cookie only over HTTPS (DOMAIN env set = production with TLS)
    return os.environ.get("DOMAIN", "") != ""


def _set_login_cookie(response, user_id: str, email: str) -> None:
    token = create_access_token(user_id, email)
    response.set_cookie(
        key="access_token", value=token,
        httponly=True, max_age=86400, samesite="lax",
        secure=_cookie_secure(),
    )


def _base_url(request: Request) -> str:
    explicit = os.environ.get("SERVER_URL")
    if explicit:
        return explicit.rstrip("/")
    return str(request.base_url).rstrip("/")


def build_reset_url(request: Request, email: str, token: str) -> str:
    return f"{_base_url(request)}/auth/password/reset?email={quote(email, safe='')}&token={token}"


def build_setup_url(request: Request, email: str, token: str) -> str:
    return f"{_base_url(request)}/auth/password/setup?email={quote(email, safe='')}&token={token}"


def _token_is_fresh(created, ttl: timedelta) -> bool:
    if not created:
        return False
    if isinstance(created, str):
        try:
            created = datetime.fromisoformat(created)
        except ValueError:
            return False
    # DuckDB returns TIMESTAMP as offset-naive; we stored it as UTC, so assume UTC.
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - created) <= ttl


def _render_message(request: Request, title: str, message: str, status_code: int = 200):
    from app.web.router import templates, _build_context
    ctx = _build_context(request, page_title=title, page_message=message)
    return templates.TemplateResponse(request, "_message.html", ctx, status_code=status_code)


def _render_reset_form(request: Request, email: str, token: str, error: str = ""):
    from app.web.router import templates, _build_context
    ctx = _build_context(request, email=email, token=token, error=error)
    return templates.TemplateResponse(request, "password_reset.html", ctx)


def _render_setup_form(request: Request, email: str, token: str, name: str = "", error: str = ""):
    from app.web.router import templates, _build_context
    ctx = _build_context(request, email=email, token=token, name=name, error=error)
    return templates.TemplateResponse(request, "password_setup.html", ctx)


def _send_mail(to_email: str, subject: str, body_text: str) -> bool:
    """Send a plaintext email via SendGrid or SMTP. Returns True on success."""
    try:
        sendgrid_key = os.environ.get("SENDGRID_API_KEY")
        if sendgrid_key:
            import sendgrid
            from sendgrid.helpers.mail import Mail
            sg = sendgrid.SendGridAPIClient(api_key=sendgrid_key)
            msg = Mail(
                from_email=os.environ.get("EMAIL_FROM_ADDRESS", "noreply@example.com"),
                to_emails=to_email,
                subject=subject,
                plain_text_content=body_text,
            )
            sg.send(msg)
            return True

        smtp_host = os.environ.get("SMTP_HOST")
        if smtp_host:
            import smtplib
            from email.mime.text import MIMEText
            msg = MIMEText(body_text)
            msg["Subject"] = subject
            msg["From"] = os.environ.get("SMTP_FROM", "noreply@example.com")
            msg["To"] = to_email
            with smtplib.SMTP(smtp_host, int(os.environ.get("SMTP_PORT", "587"))) as s:
                if os.environ.get("SMTP_USE_TLS", "true").lower() == "true":
                    s.starttls()
                smtp_user = os.environ.get("SMTP_USER")
                if smtp_user:
                    s.login(smtp_user, os.environ.get("SMTP_PASSWORD", ""))
                s.send_message(msg)
            return True
    except Exception:
        logger.exception("Failed to send mail to %s", to_email)
    return False


def send_reset_email(request: Request, email: str, token: str) -> bool:
    """Deliver a password-reset link. In LOCAL_DEV_MODE logs the link as well."""
    link = build_reset_url(request, email, token)
    if is_local_dev_mode():
        logger.warning("=" * 60)
        logger.warning("Password reset link for %s (LOCAL_DEV_MODE):", email)
        logger.warning("    %s", link)
        logger.warning("=" * 60)
    if not _has_email_transport():
        return False
    return _send_mail(email, "Reset your password", f"Click to reset your password: {link}")


def send_setup_email(request: Request, email: str, token: str) -> bool:
    link = build_setup_url(request, email, token)
    if is_local_dev_mode():
        logger.warning("=" * 60)
        logger.warning("Account setup link for %s (LOCAL_DEV_MODE):", email)
        logger.warning("    %s", link)
        logger.warning("=" * 60)
    if not _has_email_transport():
        return False
    return _send_mail(email, "Set up your account", f"Click to set up your password: {link}")


# ---- Existing flows ----

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

    try:
        ph = PasswordHasher()
        ph.verify(user["password_hash"], request.password)
    except VerifyMismatchError:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    except Exception:
        logger.exception("Unexpected error during password verification")
        raise HTTPException(status_code=500, detail="Internal server error")

    role_label = _role_label(user, conn)
    token = create_access_token(user["id"], user["email"])
    return {"access_token": token, "token_type": "bearer", "email": user["email"], "role": role_label}


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
    except VerifyMismatchError:
        # M9: audit failed form-login attempts (mirrors /auth/token endpoint)
        _audit(user["id"], "login_failed", result="invalid_password")
        return RedirectResponse(url="/login/password?error=invalid", status_code=302)
    except Exception:
        logger.exception("Unexpected error during web password verification for %s", email)
        return RedirectResponse(url="/login/password?err=auth_internal", status_code=302)

    target = next if (next.startswith("/") and not next.startswith("//")) else "/dashboard"
    response = RedirectResponse(url=target, status_code=302)
    _set_login_cookie(response, user["id"], user["email"])
    return response


# ---- JSON programmatic setup (backward compat — used by existing tests) ----

@router.post("/setup")
async def password_setup(
    request_body: PasswordSetupRequest,
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Set initial password using setup token (JSON API)."""
    repo = UserRepository(conn)
    user = repo.get_by_email(request_body.email)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.get("setup_token") != request_body.token:
        raise HTTPException(status_code=400, detail="Invalid setup token")
    if not _token_is_fresh(user.get("setup_token_created"), SETUP_TOKEN_TTL):
        raise HTTPException(status_code=400, detail="Setup token has expired")
    if not bool(user.get("active", True)):
        raise HTTPException(status_code=403, detail="Account deactivated")

    if len(request_body.password) < MIN_PASSWORD_LEN:
        raise HTTPException(status_code=400, detail=f"Password must be at least {MIN_PASSWORD_LEN} characters")

    ph = PasswordHasher()
    hashed = ph.hash(request_body.password)

    repo.update(id=user["id"], password_hash=hashed, setup_token=None, setup_token_created=None)
    token = create_access_token(user["id"], user["email"])
    return {"access_token": token, "token_type": "bearer", "message": "Password set successfully"}


# ---- Web flow: password RESET ----

@router.get("/reset", response_class=HTMLResponse)
async def reset_page(
    request: Request,
    email: str = "",
    token: str = "",
):
    """Render the 'set new password' form when arriving via reset link."""
    if not email or not token:
        return RedirectResponse(url="/login/password", status_code=302)
    return _render_reset_form(request, email=email, token=token)


@router.post("/reset")
async def reset_request(
    request: Request,
    email: str = Form(""),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Request a password-reset link. Anti-enumeration: same response regardless."""
    # Match the rest of the codebase's case-sensitive lookup (password_login,
    # email magic-link, admin create). Lowercasing here would silently fail
    # for mixed-case emails the admin stored as-is.
    email = (email or "").strip()
    if email:
        repo = UserRepository(conn)
        user = repo.get_by_email(email)
        if user and bool(user.get("active", True)):
            token = secrets.token_urlsafe(32)
            repo.update(
                id=user["id"],
                reset_token=token,
                reset_token_created=datetime.now(timezone.utc),
            )
            send_reset_email(request, email, token)
    return _render_message(
        request,
        title="Check your email",
        message="If an account exists for that email, a password-reset link has been sent. "
                "The link is valid for 24 hours.",
    )


@router.post("/reset/confirm")
async def reset_confirm(
    request: Request,
    email: str = Form(...),
    token: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Submit a new password using a reset token."""
    if password != confirm_password:
        return _render_reset_form(request, email=email, token=token, error="Passwords do not match.")
    if len(password) < MIN_PASSWORD_LEN:
        return _render_reset_form(
            request, email=email, token=token,
            error=f"Password must be at least {MIN_PASSWORD_LEN} characters.",
        )

    # Atomic compare-and-swap to consume the reset token. Mirrors the
    # magic-link CAS in app/auth/providers/email.py::_consume_token (issue
    # #82/M10) — without it, two concurrent POSTs with the same valid token
    # could both succeed in setting different new passwords. Lower
    # severity than the magic-link race (attacker would need the reset
    # token AND to race the legitimate user) but closes the asymmetry.
    cutoff = datetime.now(timezone.utc) - RESET_TOKEN_TTL
    consume_id = f"CONSUMED:{secrets.token_hex(16)}"
    try:
        conn.execute(
            "UPDATE users SET reset_token = ?, reset_token_created = NULL "
            "WHERE email = ? AND reset_token = ? AND reset_token_created IS NOT NULL "
            "AND reset_token_created >= ? AND active = TRUE",
            [consume_id, email, token, cutoff],
        )
    except Exception as exc:
        err = str(exc).lower()
        if "conflict" in err or "transaction" in err:
            return _render_reset_form(request, email=email, token=token, error="Invalid or expired reset link.")
        raise

    # Verify OUR marker won the race. A concurrent winner will have a
    # different consume_id (or NULL if they already cleared it).
    row = conn.execute(
        "SELECT reset_token FROM users WHERE email = ?",
        [email],
    ).fetchone()
    if not row or row[0] != consume_id:
        # Could be: token never matched, expired, account deactivated, or
        # the race was lost. Single error keeps the UX simple and avoids
        # leaking which condition tripped.
        return _render_reset_form(request, email=email, token=token, error="Invalid or expired reset link.")

    # Won the race — fetch the user (we need id/email for the response)
    # and apply the password change. Clearing the marker happens as part
    # of the same UPDATE.
    repo = UserRepository(conn)
    user = repo.get_by_email(email)
    if not user:
        return _render_reset_form(request, email=email, token=token, error="Invalid or expired reset link.")

    ph = PasswordHasher()
    repo.update(
        id=user["id"],
        password_hash=ph.hash(password),
        reset_token=None,
        reset_token_created=None,
    )

    response = RedirectResponse(url="/login/password?msg=password_reset", status_code=302)
    _set_login_cookie(response, user["id"], user["email"])
    return response


# ---- Web flow: initial SETUP ----

@router.get("/setup", response_class=HTMLResponse)
async def setup_page(
    request: Request,
    email: str = "",
    token: str = "",
):
    """Render the initial 'set password + name' form when arriving via invite link.

    Note: we render the form based on URL params only, without a DB lookup, so
    the response is identical for valid and invalid email/token combinations
    (anti-enumeration). Token validity is checked at POST /setup/confirm."""
    if not email or not token:
        return RedirectResponse(url="/login/password", status_code=302)
    return _render_setup_form(request, email=email, token=token)


@router.post("/setup/request")
async def setup_request(
    request: Request,
    email: str = Form(""),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Self-service 'Request Access' — emails a setup link if user is pre-approved and unset."""
    # Match the rest of the codebase's case-sensitive lookup (password_login,
    # email magic-link, admin create). Lowercasing here would silently fail
    # for mixed-case emails the admin stored as-is.
    email = (email or "").strip()
    if email:
        repo = UserRepository(conn)
        user = repo.get_by_email(email)
        # Only issue setup token if user exists, has no password yet, and is active.
        if user and not user.get("password_hash") and bool(user.get("active", True)):
            token = secrets.token_urlsafe(32)
            repo.update(
                id=user["id"],
                setup_token=token,
                setup_token_created=datetime.now(timezone.utc),
            )
            send_setup_email(request, email, token)
    return _render_message(
        request,
        title="Check your email",
        message="If your account is pre-approved, a setup link has been sent to your email. "
                "Ask an administrator if you do not receive it.",
    )


@router.post("/setup/confirm")
async def setup_confirm(
    request: Request,
    email: str = Form(...),
    token: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    name: str = Form(""),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Web form: complete initial password setup via setup token."""
    if password != confirm_password:
        return _render_setup_form(request, email=email, token=token, name=name, error="Passwords do not match.")
    if len(password) < MIN_PASSWORD_LEN:
        return _render_setup_form(
            request, email=email, token=token, name=name,
            error=f"Password must be at least {MIN_PASSWORD_LEN} characters.",
        )

    repo = UserRepository(conn)
    user = repo.get_by_email(email)
    if not user or user.get("setup_token") != token:
        return _render_setup_form(request, email=email, token=token, name=name, error="Invalid or expired setup link.")
    if not _token_is_fresh(user.get("setup_token_created"), SETUP_TOKEN_TTL):
        return _render_setup_form(request, email=email, token=token, name=name, error="Setup link has expired. Ask an administrator for a new one.")
    if not bool(user.get("active", True)):
        return _render_setup_form(request, email=email, token=token, name=name, error="This account is deactivated.")

    ph = PasswordHasher()
    updates: dict = dict(
        password_hash=ph.hash(password),
        setup_token=None,
        setup_token_created=None,
    )
    if name.strip():
        updates["name"] = name.strip()
    repo.update(id=user["id"], **updates)

    response = RedirectResponse(url="/dashboard", status_code=302)
    _set_login_cookie(response, user["id"], user["email"])
    return response
