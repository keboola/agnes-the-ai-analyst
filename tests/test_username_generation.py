"""Tests for username generation from email addresses."""

import pytest

from webapp.user_service import get_username_from_email, RESERVED_USERNAMES


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
