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

from flask import Blueprint, flash, redirect, render_template, session, url_for

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


def validate_email_domain(email: str) -> bool:
    """Check if email belongs to allowed domain or whitelist.

    Allows access for:
    1. Configured allowed domain (for Google OAuth users)
    2. Whitelisted emails (for password auth external users)
    """
    if not email:
        return False
    email_lower = email.lower()

    # Check whitelist first (for password auth users)
    if email_lower in Config.ALLOWED_EMAILS:
        return True

    # Check domain (for Google OAuth users)
    domain = email_lower.split("@")[-1]
    return domain == Config.ALLOWED_DOMAIN.lower()


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
