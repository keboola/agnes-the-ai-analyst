"""
Core authentication module - shared infrastructure.

Provides:
- login_required decorator (used by all auth methods)
- validate_email_domain() (used by all auth providers)
- /login route (dynamically renders available auth providers)
- /logout route

Auth provider-specific logic lives in auth/<provider>/provider.py.
"""

import functools
import logging

from flask import Blueprint, flash, jsonify, redirect, render_template, request, session, url_for

from .config import Config

logger = logging.getLogger(__name__)

auth_bp = Blueprint("auth", __name__)


def login_required(f):
    """Decorator to require authentication for a route."""

    @functools.wraps(f)
    def decorated_function(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("auth.login"))
        return f(*args, **kwargs)

    return decorated_function


def admin_required(f):
    """Decorator to require admin privileges for a route.

    Recomputes admin status server-side on every request.
    Returns 403 JSON for API routes, redirect for HTML routes.
    """

    @functools.wraps(f)
    def decorated_function(*args, **kwargs):
        if "user" not in session:
            if request.path.startswith("/api/"):
                return jsonify({"error": "Authentication required"}), 401
            return redirect(url_for("auth.login"))

        from .user_service import check_user_exists, get_username_from_email

        email = session.get("user", {}).get("email", "")
        username = get_username_from_email(email)
        user_info = check_user_exists(username)

        if not user_info.is_admin:
            if request.path.startswith("/api/"):
                return jsonify({"error": "Admin access required"}), 403
            flash("Admin access required.", "error")
            return redirect(url_for("dashboard"))

        return f(*args, **kwargs)

    return decorated_function


def validate_email_domain(email: str) -> bool:
    """Check if email belongs to an allowed domain or whitelist.

    Allows access for:
    1. Any of the configured allowed domains (comma-separated in config)
    2. Whitelisted emails (for individually approved external users)
    """
    if not email:
        return False
    email_lower = email.lower()

    # Check whitelist first (individually approved emails)
    if email_lower in Config.ALLOWED_EMAILS:
        return True

    # Check domain against all allowed domains
    domain = email_lower.split("@")[-1]
    return domain in Config.ALLOWED_DOMAINS


@auth_bp.route("/login")
def login():
    """Show login page with dynamically discovered auth providers."""
    if "user" in session:
        return redirect(url_for("dashboard"))

    from auth import discover_providers

    providers = discover_providers()
    login_buttons = [
        p.get_login_button()
        for p in providers
        if p.get_login_button().get("visible", True)
    ]
    return render_template("login.html", login_buttons=login_buttons)


@auth_bp.route("/logout")
def logout():
    """Clear session and redirect to login."""
    email = session.get("user", {}).get("email", "unknown")
    session.clear()
    logger.info(f"User logged out: {email}")
    flash("You have been logged out.", "info")
    return redirect(url_for("auth.login"))
