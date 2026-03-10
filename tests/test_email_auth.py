"""Tests for the email magic link authentication provider."""

import pytest

from auth.email.provider import (
    EmailAuthProvider,
    _generate_magic_token,
    _verify_magic_token,
)


# ---------------------------------------------------------------------------
# Token tests
# ---------------------------------------------------------------------------

class TestMagicTokens:
    """Test magic link token generation and verification."""

    @pytest.fixture(autouse=True)
    def setup_env(self, monkeypatch):
        """Set required env vars for Config."""
        monkeypatch.setenv("WEBAPP_SECRET_KEY", "test-secret-key-for-tokens")

    def test_generate_and_verify_token(self):
        """Valid token should return the email."""
        token = _generate_magic_token("user@acme.com")
        email = _verify_magic_token(token, max_age_seconds=60)
        assert email == "user@acme.com"

    def test_token_normalizes_email(self):
        """Email should be lowercased in the token."""
        token = _generate_magic_token("User@ACME.com")
        email = _verify_magic_token(token, max_age_seconds=60)
        assert email == "user@acme.com"

    def test_expired_token_returns_none(self):
        """Expired token should return None."""
        import time
        token = _generate_magic_token("user@acme.com")
        time.sleep(2)
        email = _verify_magic_token(token, max_age_seconds=1)
        assert email is None

    def test_invalid_token_returns_none(self):
        """Tampered token should return None."""
        email = _verify_magic_token("not-a-valid-token", max_age_seconds=60)
        assert email is None

    def test_empty_token_returns_none(self):
        """Empty token should return None."""
        email = _verify_magic_token("", max_age_seconds=60)
        assert email is None


# ---------------------------------------------------------------------------
# Provider tests
# ---------------------------------------------------------------------------

class TestEmailAuthProvider:
    """Test the EmailAuthProvider class."""

    @pytest.fixture(autouse=True)
    def setup_env(self, monkeypatch):
        """Set required env vars."""
        monkeypatch.setenv("WEBAPP_SECRET_KEY", "test-secret")

    def test_provider_name(self):
        provider = EmailAuthProvider()
        assert provider.get_name() == "email"

    def test_provider_display_name(self):
        provider = EmailAuthProvider()
        assert provider.get_display_name() == "Email"

    def test_login_button_properties(self, monkeypatch):
        monkeypatch.setattr("webapp.config.Config.ALLOWED_DOMAINS", ["acme.com"])
        provider = EmailAuthProvider()
        button = provider.get_login_button()
        assert button["text"] == "Sign in with Email"
        assert button["url"] == "/login/email"
        assert button["visible"] is True
        assert button["order"] == 20
        assert "btn-email" in button["css_class"]
        assert "acme.com" in button["subtitle"]

    def test_login_button_multiple_domains(self, monkeypatch):
        monkeypatch.setattr("webapp.config.Config.ALLOWED_DOMAINS", ["acme.com", "partner.org"])
        provider = EmailAuthProvider()
        button = provider.get_login_button()
        assert "acme.com" in button["subtitle"]
        assert "partner.org" in button["subtitle"]

    def test_provider_available_with_domain(self, monkeypatch):
        monkeypatch.setattr("webapp.config.Config.ALLOWED_DOMAINS", ["acme.com"])
        provider = EmailAuthProvider()
        assert provider.is_available() is True

    def test_provider_unavailable_without_domain(self, monkeypatch):
        monkeypatch.setattr("webapp.config.Config.ALLOWED_DOMAINS", [])
        provider = EmailAuthProvider()
        assert provider.is_available() is False
