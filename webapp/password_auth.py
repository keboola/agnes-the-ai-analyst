"""
Password-based authentication for whitelisted external users.

Provides email/password login as an alternative to Google OAuth
for users who don't have internal domain accounts.
"""

import json
import logging
import secrets
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from time import time
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from .config import Config
from .email_service import send_reset_email, send_setup_email

logger = logging.getLogger(__name__)

password_auth_bp = Blueprint("password_auth", __name__)

# Argon2 password hasher with recommended parameters
ph = PasswordHasher()

# Rate limiting: track failed login attempts per email
# Format: {email: [(timestamp, ...)] }
_login_attempts: dict[str, list[float]] = defaultdict(list)
MAX_LOGIN_ATTEMPTS = 5
LOGIN_ATTEMPT_WINDOW = 60  # seconds


def _is_rate_limited(email: str) -> bool:
    """Check if email is rate limited due to too many failed attempts."""
    now = time()
    # Clean old attempts
    _login_attempts[email] = [
        t for t in _login_attempts[email] if now - t < LOGIN_ATTEMPT_WINDOW
    ]
    return len(_login_attempts[email]) >= MAX_LOGIN_ATTEMPTS


def _record_failed_attempt(email: str) -> None:
    """Record a failed login attempt for rate limiting."""
    _login_attempts[email].append(time())


def _clear_attempts(email: str) -> None:
    """Clear login attempts after successful login."""
    _login_attempts.pop(email, None)


def _load_users() -> dict[str, Any]:
    """Load users from JSON storage."""
    if not Config.PASSWORD_USERS_FILE.exists():
        return {}
    try:
        with open(Config.PASSWORD_USERS_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"Failed to load password users: {e}")
        return {}


def _save_users(users: dict[str, Any]) -> bool:
    """Save users to JSON storage."""
    try:
        # Ensure directory exists
        Config.PASSWORD_USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
        # Write atomically using temp file
        temp_file = Config.PASSWORD_USERS_FILE.with_suffix(".tmp")
        with open(temp_file, "w") as f:
            json.dump(users, f, indent=2)
        temp_file.replace(Config.PASSWORD_USERS_FILE)
        return True
    except OSError as e:
        logger.error(f"Failed to save password users: {e}")
        return False


def _is_whitelisted(email: str) -> bool:
    """Check if email is in the whitelist."""
    return email.lower() in Config.ALLOWED_EMAILS


def _get_user(email: str) -> dict[str, Any] | None:
    """Get user data by email."""
    users = _load_users()
    return users.get(email.lower())


def _create_or_update_user(email: str, data: dict[str, Any]) -> bool:
    """Create or update user in storage."""
    users = _load_users()
    email_lower = email.lower()
    if email_lower in users:
        users[email_lower].update(data)
    else:
        users[email_lower] = data
    return _save_users(users)


def _generate_token() -> str:
    """Generate a secure random token."""
    return secrets.token_urlsafe(32)


def _hash_password(password: str) -> str:
    """Hash password using Argon2id."""
    return ph.hash(password)


def _verify_password(password_hash: str, password: str) -> bool:
    """Verify password against hash."""
    try:
        ph.verify(password_hash, password)
        return True
    except VerifyMismatchError:
        return False


def _get_base_url() -> str:
    """Get base URL for email links."""
    return request.url_root.rstrip("/")


def _validate_password(password: str) -> tuple[bool, str]:
    """Validate password meets requirements.

    Requirements:
    - At least 8 characters
    - At least one uppercase letter
    - At least one lowercase letter
    - At least one digit
    """
    if len(password) < 8:
        return False, "Password must be at least 8 characters long"
    if not any(c.isupper() for c in password):
        return False, "Password must contain at least one uppercase letter"
    if not any(c.islower() for c in password):
        return False, "Password must contain at least one lowercase letter"
    if not any(c.isdigit() for c in password):
        return False, "Password must contain at least one digit"
    return True, ""


@password_auth_bp.route("/login/email", methods=["GET", "POST"])
def login_email():
    """Email login form and handling."""
    if "user" in session:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "").strip()

        # Check rate limiting
        if _is_rate_limited(email):
            flash("Too many login attempts. Please wait a minute and try again.", "error")
            return render_template("login_email.html", email=email)

        if not email:
            flash("Email is required.", "error")
            return render_template("login_email.html")

        # Check if email is whitelisted
        if not _is_whitelisted(email):
            flash("This email is not authorized to access this platform.", "error")
            _record_failed_attempt(email)
            return render_template("login_email.html", email=email)

        user = _get_user(email)

        # If user doesn't exist or has no password, show request access option
        if not user or not user.get("password_hash"):
            flash(
                "No account found for this email. Click 'Request Access' to set up your account.",
                "info",
            )
            return render_template("login_email.html", email=email, show_request_access=True)

        if not password:
            flash("Password is required.", "error")
            return render_template("login_email.html", email=email)

        # Verify password
        if not _verify_password(user["password_hash"], password):
            _record_failed_attempt(email)
            flash("Invalid email or password.", "error")
            return render_template("login_email.html", email=email)

        # Successful login
        _clear_attempts(email)

        # Update last login
        _create_or_update_user(
            email,
            {"last_login": datetime.now(timezone.utc).isoformat()},
        )

        # Create session (same format as Google OAuth)
        session["user"] = {
            "email": email,
            "name": user.get("name", email.split("@")[0]),
            "picture": "",  # No picture for password auth users
        }

        logger.info(f"Password auth login: {email}")
        return redirect(url_for("dashboard"))

    return render_template("login_email.html")


@password_auth_bp.route("/auth/request-access", methods=["POST"])
def request_access():
    """Request access (send setup email) for whitelisted email."""
    email = request.form.get("email", "").strip().lower()

    if not email:
        flash("Email is required.", "error")
        return redirect(url_for("password_auth.login_email"))

    # Check if email is whitelisted
    if not _is_whitelisted(email):
        flash("This email is not authorized to access this platform.", "error")
        return redirect(url_for("password_auth.login_email"))

    user = _get_user(email)

    # If user already has a password, redirect to login
    if user and user.get("password_hash"):
        flash("You already have an account. Please log in.", "info")
        return redirect(url_for("password_auth.login_email"))

    # Generate setup token
    token = _generate_token()
    expiry = datetime.now(timezone.utc).timestamp() + Config.SETUP_TOKEN_EXPIRY

    # Save token to user record
    _create_or_update_user(
        email,
        {
            "setup_token": token,
            "setup_token_expiry": expiry,
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
    )

    # Send setup email
    success, message = send_setup_email(email, token, _get_base_url())
    if success:
        flash(
            "We've sent you an email with instructions to set up your account. "
            "Please check your inbox.",
            "success",
        )
    else:
        logger.error(f"Failed to send setup email to {email}: {message}")
        flash("Failed to send setup email. Please try again later.", "error")

    return redirect(url_for("password_auth.login_email"))


@password_auth_bp.route("/auth/setup/<token>", methods=["GET", "POST"])
def setup_password(token: str):
    """Setup password form and handling."""
    # Find user with this token
    users = _load_users()
    target_email = None
    target_user = None

    for email, user_data in users.items():
        if secrets.compare_digest(user_data.get("setup_token") or "", token):
            target_email = email
            target_user = user_data
            break

    if not target_email or not target_user:
        flash("Invalid or expired setup link.", "error")
        return redirect(url_for("password_auth.login_email"))

    # Check token expiry
    expiry = target_user.get("setup_token_expiry", 0)
    if datetime.now(timezone.utc).timestamp() > expiry:
        flash("This setup link has expired. Please request a new one.", "error")
        return redirect(url_for("password_auth.login_email"))

    if request.method == "POST":
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        name = request.form.get("name", "").strip()

        # Validate password
        is_valid, error = _validate_password(password)
        if not is_valid:
            flash(error, "error")
            return render_template("password_setup.html", email=target_email, name=name)

        if password != confirm_password:
            flash("Passwords do not match.", "error")
            return render_template("password_setup.html", email=target_email, name=name)

        # Hash password and save
        password_hash = _hash_password(password)
        _create_or_update_user(
            target_email,
            {
                "password_hash": password_hash,
                "name": name or target_email.split("@")[0],
                "setup_token": None,
                "setup_token_expiry": None,
            },
        )

        logger.info(f"Password set up for: {target_email}")
        flash("Your password has been set up successfully. You can now log in.", "success")
        return redirect(url_for("password_auth.login_email"))

    return render_template("password_setup.html", email=target_email)


@password_auth_bp.route("/auth/reset-request", methods=["POST"])
def reset_request():
    """Request password reset email."""
    email = request.form.get("email", "").strip().lower()

    if not email:
        flash("Email is required.", "error")
        return redirect(url_for("password_auth.login_email"))

    # Check if email is whitelisted and has an account
    if not _is_whitelisted(email):
        # Don't reveal whether email is whitelisted
        flash(
            "If this email is registered, you will receive a password reset link.",
            "info",
        )
        return redirect(url_for("password_auth.login_email"))

    user = _get_user(email)
    if not user or not user.get("password_hash"):
        # Don't reveal whether account exists
        flash(
            "If this email is registered, you will receive a password reset link.",
            "info",
        )
        return redirect(url_for("password_auth.login_email"))

    # Generate reset token
    token = _generate_token()
    expiry = datetime.now(timezone.utc).timestamp() + Config.RESET_TOKEN_EXPIRY

    # Save token to user record
    _create_or_update_user(
        email,
        {
            "reset_token": token,
            "reset_token_expiry": expiry,
        },
    )

    # Send reset email
    success, message = send_reset_email(email, token, _get_base_url())
    if not success:
        logger.error(f"Failed to send reset email to {email}: {message}")

    # Always show same message to prevent email enumeration
    flash(
        "If this email is registered, you will receive a password reset link.",
        "info",
    )
    return redirect(url_for("password_auth.login_email"))


@password_auth_bp.route("/auth/reset/<token>", methods=["GET", "POST"])
def reset_password(token: str):
    """Reset password form and handling."""
    # Find user with this token
    users = _load_users()
    target_email = None
    target_user = None

    for email, user_data in users.items():
        if secrets.compare_digest(user_data.get("reset_token") or "", token):
            target_email = email
            target_user = user_data
            break

    if not target_email or not target_user:
        flash("Invalid or expired reset link.", "error")
        return redirect(url_for("password_auth.login_email"))

    # Check token expiry
    expiry = target_user.get("reset_token_expiry", 0)
    if datetime.now(timezone.utc).timestamp() > expiry:
        flash("This reset link has expired. Please request a new one.", "error")
        return redirect(url_for("password_auth.login_email"))

    if request.method == "POST":
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

        # Validate password
        is_valid, error = _validate_password(password)
        if not is_valid:
            flash(error, "error")
            return render_template("password_reset.html", email=target_email)

        if password != confirm_password:
            flash("Passwords do not match.", "error")
            return render_template("password_reset.html", email=target_email)

        # Hash password and save
        password_hash = _hash_password(password)
        _create_or_update_user(
            target_email,
            {
                "password_hash": password_hash,
                "reset_token": None,
                "reset_token_expiry": None,
            },
        )

        logger.info(f"Password reset for: {target_email}")
        flash("Your password has been reset successfully. You can now log in.", "success")
        return redirect(url_for("password_auth.login_email"))

    return render_template("password_reset.html", email=target_email)
