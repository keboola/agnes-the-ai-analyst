"""
Google OAuth authentication module.

Handles Google Sign-In flow and domain validation.
"""

import functools
import logging

from authlib.integrations.flask_client import OAuth
from flask import Blueprint, current_app, flash, redirect, session, url_for

from .config import Config

logger = logging.getLogger(__name__)

auth_bp = Blueprint("auth", __name__)
oauth = OAuth()


def init_oauth(app):
    """Initialize OAuth with the Flask app."""
    oauth.init_app(app)

    oauth.register(
        name="google",
        client_id=Config.GOOGLE_CLIENT_ID,
        client_secret=Config.GOOGLE_CLIENT_SECRET,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={
            "scope": "openid email profile",
        },
    )


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
    """Show login page or redirect to dashboard if already logged in."""
    if "user" in session:
        return redirect(url_for("dashboard"))
    from flask import render_template

    return render_template("login.html")


@auth_bp.route("/login/google")
def login_google():
    """Initiate Google OAuth flow."""
    redirect_uri = url_for("auth.authorize", _external=True)
    return oauth.google.authorize_redirect(redirect_uri)


@auth_bp.route("/authorize")
def authorize():
    """Handle OAuth callback from Google."""
    try:
        token = oauth.google.authorize_access_token()
        userinfo = token.get("userinfo")

        if not userinfo:
            logger.warning("No userinfo in OAuth response")
            flash("Failed to get user information from Google.", "error")
            return redirect(url_for("auth.login"))

        email = userinfo.get("email", "")
        name = userinfo.get("name", "")

        # Validate domain
        if not validate_email_domain(email):
            logger.warning(f"Login attempt from non-allowed domain: {email}")
            flash(
                f"Only @{Config.ALLOWED_DOMAIN} email addresses are allowed.", "error"
            )
            return redirect(url_for("auth.login"))

        # Store user info in session
        session["user"] = {
            "email": email,
            "name": name,
            "picture": userinfo.get("picture", ""),
        }

        logger.info(f"User logged in: {email}")
        return redirect(url_for("dashboard"))

    except Exception as e:
        logger.exception(f"OAuth error: {e}")
        flash("Authentication failed. Please try again.", "error")
        return redirect(url_for("auth.login"))


@auth_bp.route("/logout")
def logout():
    """Clear session and redirect to login."""
    email = session.get("user", {}).get("email", "unknown")
    session.clear()
    logger.info(f"User logged out: {email}")
    flash("You have been logged out.", "info")
    return redirect(url_for("auth.login"))
