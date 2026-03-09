"""
Pluggable authentication provider system.

Each auth provider lives in auth/<name>/provider.py and implements AuthProvider.
Providers are auto-discovered and registered with the Flask app.

To add a new provider (e.g., Okta):
1. Create auth/okta/provider.py
2. Implement AuthProvider subclass
3. Export `provider` instance at module level
4. That's it - no changes to core code needed.
"""

import importlib
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from flask import Blueprint

logger = logging.getLogger(__name__)


class AuthProvider(ABC):
    """Base class for authentication providers."""

    @abstractmethod
    def get_name(self) -> str:
        """Internal name (e.g., 'google', 'password')."""

    @abstractmethod
    def get_blueprint(self) -> Blueprint:
        """Flask blueprint with auth routes."""

    @abstractmethod
    def get_login_button(self) -> dict:
        """Login button definition for the login page.

        Returns dict with:
            text: str - Button label (e.g., "Sign in with Google")
            url: str - Route URL (e.g., "/login/google")
            icon_html: str - SVG or HTML for the icon
            subtitle: str - Optional help text below button
            order: int - Sort order (lower = higher on page)
            css_class: str - Optional CSS class for the button (e.g., "btn-google")
            visible: bool - Whether to show on login page (default True)
        """

    def is_available(self) -> bool:
        """Check if provider is configured and ready.
        Override to check env vars, API keys, etc.
        Returns False to skip this provider."""
        return True

    def get_display_name(self) -> str:
        """Human-readable name for UI."""
        return self.get_name().title()

    def init_app(self, app) -> None:
        """Optional: initialize provider with Flask app (e.g., for OAuth setup)."""
        pass


def discover_providers() -> list[AuthProvider]:
    """Auto-discover auth providers from auth/*/provider.py.

    Each provider module must export a `provider` instance of AuthProvider.
    Providers are sorted by login button order.
    Only available providers (is_available() == True) are returned.
    """
    providers = []
    auth_dir = Path(__file__).parent

    for subdir in sorted(auth_dir.iterdir()):
        if not subdir.is_dir() or subdir.name.startswith("_"):
            continue
        provider_file = subdir / "provider.py"
        if not provider_file.exists():
            continue

        try:
            mod = importlib.import_module(f"auth.{subdir.name}.provider")
            provider_instance = getattr(mod, "provider", None)
            if provider_instance and isinstance(provider_instance, AuthProvider):
                if provider_instance.is_available():
                    providers.append(provider_instance)
                    logger.info(f"Auth provider loaded: {provider_instance.get_name()}")
                else:
                    logger.debug(
                        f"Auth provider skipped (not available): {subdir.name}"
                    )
            else:
                logger.warning(
                    f"Auth provider {subdir.name} has no 'provider' instance"
                )
        except Exception as e:
            logger.warning(f"Failed to load auth provider {subdir.name}: {e}")

    # Sort by login button order
    providers.sort(key=lambda p: p.get_login_button().get("order", 50))
    return providers
