"""
Google OAuth authentication provider.

Handles Google Sign-In flow with domain validation.
Google OAuth flow with domain validation (Flask blueprint).
"""

import logging

from authlib.integrations.flask_client import OAuth
from flask import Blueprint, flash, redirect, session, url_for

import os

from auth import AuthProvider
from app.instance_config import get_allowed_domains

_ALLOWED_DOMAINS = get_allowed_domains()
_ALLOWED_EMAILS = [
    e.strip().lower()
    for e in os.environ.get("ALLOWED_EMAILS", "").split(",")
    if e.strip()
]


def validate_email_domain(email: str) -> bool:
    if not email:
        return False
    email_lower = email.lower()
    if email_lower in _ALLOWED_EMAILS:
        return True
    domain = email_lower.split("@")[-1]
    return domain in _ALLOWED_DOMAINS


class _Config:
    ALLOWED_DOMAINS = _ALLOWED_DOMAINS
    GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
    GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")


Config = _Config

logger = logging.getLogger(__name__)

google_bp = Blueprint("google_auth", __name__)
oauth = OAuth()

# Google SVG icon for the login button
_GOOGLE_ICON_HTML = (
    '<svg class="google-icon" viewBox="0 0 24 24" width="24" height="24">'
    '<path fill="#4285F4" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 '
    "1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z\"/>"
    '<path fill="#34A853" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 '
    "1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z\"/>"
    '<path fill="#FBBC05" d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07'
    'H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z"/>'
    '<path fill="#EA4335" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 '
    '14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z"/>'
    "</svg>"
)


@google_bp.route("/login/google")
def login_google():
    """Initiate Google OAuth flow."""
    redirect_uri = url_for("google_auth.authorize", _external=True)
    return oauth.google.authorize_redirect(redirect_uri)


@google_bp.route("/authorize")
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
            domains_str = ", ".join(f"@{d}" for d in Config.ALLOWED_DOMAINS)
            flash(
                f"Only {domains_str} email addresses are allowed.", "error"
            )
            return redirect(url_for("auth.login"))

        # Store user info in session (shared contract across all providers)
        session["user"] = {
            "email": email,
            "name": name,
            "picture": userinfo.get("picture", ""),
        }

        logger.info(f"User logged in via Google: {email}")
        return redirect(url_for("dashboard"))

    except Exception as e:
        logger.exception(f"OAuth error: {e}")
        flash("Authentication failed. Please try again.", "error")
        return redirect(url_for("auth.login"))


class GoogleAuthProvider(AuthProvider):
    """Google OAuth authentication provider."""

    def get_name(self) -> str:
        return "google"

    def get_display_name(self) -> str:
        return "Google"

    def get_blueprint(self) -> Blueprint:
        return google_bp

    def get_login_button(self) -> dict:
        domains = Config.ALLOWED_DOMAINS
        if len(domains) > 1:
            domain_str = ", ".join(f"@{d}" for d in domains)
        else:
            domain_str = f"@{domains[0]}" if domains else ""
        return {
            "text": "Sign in with Google",
            "url": "/login/google",
            "icon_html": _GOOGLE_ICON_HTML,
            "subtitle": f'For <strong>{domain_str}</strong> email addresses.' if domain_str else "",
            "order": 10,
            "css_class": "btn-google",
            "visible": True,
        }

    def is_available(self) -> bool:
        return bool(Config.GOOGLE_CLIENT_ID)

    def init_app(self, app) -> None:
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


# Module-level provider instance for auto-discovery
provider = GoogleAuthProvider()
