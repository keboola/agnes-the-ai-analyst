"""
Email/password authentication provider.

Email/password authentication (Flask blueprint).
Available only when SENDGRID_API_KEY is configured.
"""

import logging

from flask import Blueprint

import os

from auth import AuthProvider


class _Config:
    SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY", "")


Config = _Config

logger = logging.getLogger(__name__)


class PasswordAuthProvider(AuthProvider):
    """Email/password authentication provider for external users."""

    def get_name(self) -> str:
        return "password"

    def get_display_name(self) -> str:
        return "Email"

    def get_blueprint(self) -> Blueprint:
        # Legacy Flask blueprint — removed with webapp/
        return Blueprint("password_auth", __name__)

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
