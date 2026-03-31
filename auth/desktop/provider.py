"""
Desktop JWT authentication provider.

Desktop JWT authentication (Flask blueprint).
This is NOT a login provider (no login button) - it provides
JWT-based API authentication for the native desktop application.
"""

import logging

from flask import Blueprint

import os

from auth import AuthProvider


class _Config:
    DESKTOP_JWT_SECRET = os.environ.get("DESKTOP_JWT_SECRET", "")


Config = _Config

logger = logging.getLogger(__name__)


class DesktopAuthProvider(AuthProvider):
    """Desktop app JWT authentication provider."""

    def get_name(self) -> str:
        return "desktop"

    def get_display_name(self) -> str:
        return "Desktop App"

    def get_blueprint(self) -> Blueprint:
        # Legacy Flask blueprint — removed with webapp/
        return Blueprint("desktop_auth", __name__)

    def get_login_button(self) -> dict:
        return {
            "text": "",
            "url": "",
            "icon_html": "",
            "subtitle": "",
            "order": 100,
            "css_class": "",
            "visible": False,
        }

    def is_available(self) -> bool:
        return bool(Config.DESKTOP_JWT_SECRET)

    def init_app(self, app) -> None:
        """No additional initialization needed."""
        pass


# Module-level provider instance for auto-discovery
provider = DesktopAuthProvider()
