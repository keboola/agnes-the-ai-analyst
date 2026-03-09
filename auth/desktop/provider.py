"""
Desktop JWT authentication provider.

Wraps the existing webapp/desktop_auth.py blueprint.
This is NOT a login provider (no login button) - it provides
JWT-based API authentication for the native desktop application.
"""

import logging

from flask import Blueprint

from auth import AuthProvider
from webapp.config import Config

logger = logging.getLogger(__name__)


class DesktopAuthProvider(AuthProvider):
    """Desktop app JWT authentication provider."""

    def get_name(self) -> str:
        return "desktop"

    def get_display_name(self) -> str:
        return "Desktop App"

    def get_blueprint(self) -> Blueprint:
        from webapp.desktop_auth import desktop_bp

        return desktop_bp

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
