"""
Email/password authentication provider.

Wraps the existing webapp/password_auth.py blueprint.
Available only when SENDGRID_API_KEY is configured.
"""

import logging

from flask import Blueprint

from auth import AuthProvider
from webapp.config import Config

logger = logging.getLogger(__name__)


class PasswordAuthProvider(AuthProvider):
    """Email/password authentication provider for external users."""

    def get_name(self) -> str:
        return "password"

    def get_display_name(self) -> str:
        return "Email"

    def get_blueprint(self) -> Blueprint:
        from webapp.password_auth import password_auth_bp

        return password_auth_bp

    def get_login_button(self) -> dict:
        return {
            "text": "Sign in with Email",
            "url": "/login/email",
            "icon_html": "",
            "subtitle": "For external users (investors, partners).",
            "order": 20,
            "css_class": "btn-secondary",
            "visible": True,
        }

    def is_available(self) -> bool:
        return bool(Config.SENDGRID_API_KEY)

    def init_app(self, app) -> None:
        """No additional initialization needed."""
        pass


# Module-level provider instance for auto-discovery
provider = PasswordAuthProvider()
