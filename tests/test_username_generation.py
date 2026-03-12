"""Tests for username generation from email addresses."""

import pytest

from webapp.user_service import get_username_from_email, get_webapp_username, RESERVED_USERNAMES
import webapp.user_service as user_service_module


class TestGetUsernameFromEmail:
    """Test email-to-username conversion."""

    def test_basic_email(self):
        assert get_username_from_email("admin@test.com") == "admin_test_com"

    def test_email_with_dots(self):
        assert get_username_from_email("john.doe@acme.com") == "john_doe_acme_com"

    def test_different_domains_produce_different_usernames(self):
        """Same local part, different domains -> unique usernames."""
        u1 = get_username_from_email("pavel@test.com")
        u2 = get_username_from_email("pavel@groupon.com")
        u3 = get_username_from_email("pavel@keboola.com")
        assert u1 == "pavel_test_com"
        assert u2 == "pavel_groupon_com"
        assert u3 == "pavel_keboola_com"
        assert len({u1, u2, u3}) == 3  # all unique

    def test_email_normalized_to_lowercase(self):
        assert get_username_from_email("Admin@Test.COM") == "admin_test_com"

    def test_empty_email(self):
        assert get_username_from_email("") == ""

    def test_no_at_sign(self):
        assert get_username_from_email("notanemail") == ""

    def test_none_email(self):
        assert get_username_from_email(None) == ""

    def test_reserved_names_avoided(self):
        """Usernames from emails should NOT collide with reserved names."""
        # admin@anything.com -> admin_anything_com (not 'admin')
        username = get_username_from_email("admin@company.com")
        assert username not in RESERVED_USERNAMES
        assert username == "admin_company_com"

    def test_test_email_not_reserved(self):
        username = get_username_from_email("test@company.com")
        assert username not in RESERVED_USERNAMES

    def test_subdomain_email(self):
        assert get_username_from_email("user@mail.acme.co.uk") == "user_mail_acme_co_uk"


class TestGetWebappUsername:
    """Test get_webapp_username() with configurable prefix and domain stripping."""

    def test_prefix_and_strip_domain(self, monkeypatch):
        monkeypatch.setattr(user_service_module, "_USERNAME_PREFIX", "foundry_")
        monkeypatch.setattr(user_service_module, "_USERNAME_STRIP_DOMAIN", True)
        assert get_webapp_username("e.psimecek@groupon.com") == "foundry_e_psimecek"

    def test_prefix_no_strip(self, monkeypatch):
        monkeypatch.setattr(user_service_module, "_USERNAME_PREFIX", "foundry_")
        monkeypatch.setattr(user_service_module, "_USERNAME_STRIP_DOMAIN", False)
        assert get_webapp_username("e.psimecek@groupon.com") == "foundry_e_psimecek_groupon_com"

    def test_no_prefix_strip_domain(self, monkeypatch):
        monkeypatch.setattr(user_service_module, "_USERNAME_PREFIX", "")
        monkeypatch.setattr(user_service_module, "_USERNAME_STRIP_DOMAIN", True)
        assert get_webapp_username("e.psimecek@groupon.com") == "e_psimecek"

    def test_legacy_no_options(self, monkeypatch):
        monkeypatch.setattr(user_service_module, "_USERNAME_PREFIX", "")
        monkeypatch.setattr(user_service_module, "_USERNAME_STRIP_DOMAIN", False)
        assert get_webapp_username("e.psimecek@groupon.com") == "e_psimecek_groupon_com"

    def test_empty_email(self, monkeypatch):
        monkeypatch.setattr(user_service_module, "_USERNAME_PREFIX", "foundry_")
        monkeypatch.setattr(user_service_module, "_USERNAME_STRIP_DOMAIN", True)
        assert get_webapp_username("") == ""

    def test_none_email(self, monkeypatch):
        monkeypatch.setattr(user_service_module, "_USERNAME_PREFIX", "foundry_")
        monkeypatch.setattr(user_service_module, "_USERNAME_STRIP_DOMAIN", True)
        assert get_webapp_username(None) == ""

    def test_no_at_sign(self, monkeypatch):
        monkeypatch.setattr(user_service_module, "_USERNAME_PREFIX", "foundry_")
        monkeypatch.setattr(user_service_module, "_USERNAME_STRIP_DOMAIN", True)
        assert get_webapp_username("notanemail") == ""

    def test_uppercase_normalized(self, monkeypatch):
        monkeypatch.setattr(user_service_module, "_USERNAME_PREFIX", "app_")
        monkeypatch.setattr(user_service_module, "_USERNAME_STRIP_DOMAIN", True)
        assert get_webapp_username("John.Doe@ACME.COM") == "app_john_doe"

    def test_strip_domain_multiple_dots(self, monkeypatch):
        monkeypatch.setattr(user_service_module, "_USERNAME_PREFIX", "")
        monkeypatch.setattr(user_service_module, "_USERNAME_STRIP_DOMAIN", True)
        assert get_webapp_username("first.middle.last@company.com") == "first_middle_last"
