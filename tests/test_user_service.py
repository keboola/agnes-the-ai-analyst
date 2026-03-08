"""Tests for webapp.user_service SSH key validation and normalization."""

import pytest

from webapp.user_service import validate_ssh_key


# Valid SSH keys for testing
VALID_ED25519_KEY = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDIE6+wG3U019D4AUQVOr17xxX5enS1OUVfLZ4cHa4/O"
    " user@hostname"
)

VALID_RSA_KEY = (
    "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQC7vbqajxMM8qR"
    "ZortjjMq2bCxPkNW0KSBiAIae6MNcL+Kk6MFSwj3pvFOyEEBO4e"
    "OE1JxHoOdSPsFhPeQisdit3MfG6E7CaJm3VE/ONxQat1M/V+55og"
    " user@hostname"
)


class TestValidateSshKey:
    """Test SSH key validation."""

    def test_valid_ed25519_key(self):
        is_valid, error = validate_ssh_key(VALID_ED25519_KEY)
        assert is_valid
        assert error == ""

    def test_valid_rsa_key(self):
        is_valid, error = validate_ssh_key(VALID_RSA_KEY)
        assert is_valid
        assert error == ""

    def test_empty_key(self):
        is_valid, error = validate_ssh_key("")
        assert not is_valid
        assert "required" in error.lower()

    def test_private_key_rejected(self):
        is_valid, error = validate_ssh_key("-----BEGIN PRIVATE KEY-----\ndata\n-----END PRIVATE KEY-----")
        assert not is_valid

    def test_too_short_key(self):
        is_valid, error = validate_ssh_key("ssh-ed25519 AAAA user@host")
        assert not is_valid
        assert "short" in error.lower()

    def test_invalid_format(self):
        is_valid, error = validate_ssh_key("not-a-valid-ssh-key-at-all")
        assert not is_valid


class TestSshKeyNewlineNormalization:
    """Test that SSH keys with line breaks are normalized to single line.

    This is the core fix for issue #139: webapp was writing keys with
    line breaks to authorized_keys, causing SSH auth failures.
    """

    def test_key_with_newlines_between_parts(self):
        """Key with newlines between type, data, and comment."""
        broken_key = (
            "ssh-ed25519\n"
            "AAAAC3NzaC1lZDI1NTE5AAAAIDIE6+wG3U019D4AUQVOr17xxX5enS1OUVfLZ4cHa4/O\n"
            "user@hostname"
        )
        is_valid, error = validate_ssh_key(broken_key)
        assert is_valid, f"Key with newlines should be valid after normalization: {error}"

    def test_key_with_carriage_returns(self):
        """Key with Windows-style \\r\\n line breaks."""
        broken_key = (
            "ssh-ed25519\r\n"
            "AAAAC3NzaC1lZDI1NTE5AAAAIDIE6+wG3U019D4AUQVOr17xxX5enS1OUVfLZ4cHa4/O\r\n"
            "user@hostname"
        )
        is_valid, error = validate_ssh_key(broken_key)
        assert is_valid, f"Key with \\r\\n should be valid after normalization: {error}"

    def test_key_with_tabs(self):
        """Key with tabs instead of spaces."""
        broken_key = (
            "ssh-ed25519\t"
            "AAAAC3NzaC1lZDI1NTE5AAAAIDIE6+wG3U019D4AUQVOr17xxX5enS1OUVfLZ4cHa4/O\t"
            "user@hostname"
        )
        is_valid, error = validate_ssh_key(broken_key)
        assert is_valid, f"Key with tabs should be valid after normalization: {error}"

    def test_key_with_multiple_spaces(self):
        """Key with extra spaces between parts."""
        broken_key = (
            "ssh-ed25519   "
            "AAAAC3NzaC1lZDI1NTE5AAAAIDIE6+wG3U019D4AUQVOr17xxX5enS1OUVfLZ4cHa4/O   "
            "user@hostname"
        )
        is_valid, error = validate_ssh_key(broken_key)
        assert is_valid, f"Key with extra spaces should be valid after normalization: {error}"

    def test_key_with_leading_trailing_whitespace(self):
        """Key with leading/trailing whitespace and newlines."""
        broken_key = (
            "\n  ssh-ed25519 "
            "AAAAC3NzaC1lZDI1NTE5AAAAIDIE6+wG3U019D4AUQVOr17xxX5enS1OUVfLZ4cHa4/O "
            "user@hostname  \n"
        )
        is_valid, error = validate_ssh_key(broken_key)
        assert is_valid, f"Key with surrounding whitespace should be valid: {error}"

    def test_key_without_comment_with_newlines(self):
        """Key without comment part, with newlines."""
        broken_key = (
            "ssh-ed25519\n"
            "AAAAC3NzaC1lZDI1NTE5AAAAIDIE6+wG3U019D4AUQVOr17xxX5enS1OUVfLZ4cHa4/O\n"
        )
        is_valid, error = validate_ssh_key(broken_key)
        assert is_valid, f"Key without comment with newlines should be valid: {error}"
