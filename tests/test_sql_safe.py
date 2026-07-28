"""Tests for src.sql_safe — identifier and project_id validators."""

import pytest

from src.sql_safe import (
    is_safe_identifier,
    is_safe_project_id,
    validate_identifier,
    validate_project_id,
)


class TestIsSafeIdentifier:
    @pytest.mark.parametrize("name", ["orders", "T_1", "_x", "a" * 64])
    def test_accepts_valid_identifiers(self, name):
        assert is_safe_identifier(name) is True

    @pytest.mark.parametrize(
        "name",
        [
            "",
            "1leading_digit",
            "has space",
            "has-dash",
            "has.dot",
            "has;semicolon",
            "has'quote",
            "has\"doublequote",
            "has`backtick",
            "a" * 65,  # too long
        ],
    )
    def test_rejects_unsafe_identifiers(self, name):
        assert is_safe_identifier(name) is False

    def test_rejects_non_string(self):
        assert is_safe_identifier(None) is False
        assert is_safe_identifier(123) is False


class TestIsSafeProjectId:
    @pytest.mark.parametrize(
        "pid",
        [
            "abcdef",                # 6 chars (minimum)
            "my-project",            # standard form
            "abc12-345",             # mid hyphen + digits
            "a" + "b" * 28 + "c",    # 30 chars (maximum)
        ],
    )
    def test_accepts_valid_project_ids(self, pid):
        assert is_safe_project_id(pid) is True

    @pytest.mark.parametrize(
        "pid",
        [
            "",
            "abc",                       # too short
            "ABC123",                    # uppercase rejected
            "1leading-digit",            # must start with letter
            "trailing-",                 # cannot end with hyphen
            "has_underscore",            # underscore not allowed
            "a" * 31,                    # too long
            "has space",
            "has.dot",
            "has;semicolon",
            "has'quote",
            "evil'; DROP TABLE foo; --",
        ],
    )
    def test_rejects_unsafe_project_ids(self, pid):
        assert is_safe_project_id(pid) is False

    def test_rejects_non_string(self):
        assert is_safe_project_id(None) is False
        assert is_safe_project_id(42) is False


class TestValidateIdentifier:
    def test_returns_true_and_no_warning_on_valid(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING):
            assert validate_identifier("orders", "table_name") is True
        assert caplog.records == []

    def test_returns_false_and_warns_on_invalid(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING, logger="src.sql_safe"):
            assert validate_identifier("evil; DROP", "table_name") is False
        assert any("table_name" in r.message for r in caplog.records)


class TestValidateProjectId:
    def test_returns_true_and_no_warning_on_valid(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING):
            assert validate_project_id("my-project") is True
        assert caplog.records == []

    def test_returns_false_and_warns_on_invalid(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING, logger="src.sql_safe"):
            assert validate_project_id("evil'; DROP TABLE foo; --") is False
        assert any("project_id" in r.message for r in caplog.records)
