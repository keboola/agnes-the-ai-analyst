"""Unit tests for connectors/jira/validation.py — issue #83 defenses."""

import pytest

from connectors.jira.validation import is_valid_issue_key, safe_join_under


class TestIsValidIssueKey:
    @pytest.mark.parametrize("key", ["TEST-1", "PROJ-42", "ABC-123", "AB1-9", "A-1", "AB42-1234567"])
    def test_valid(self, key):
        assert is_valid_issue_key(key) is True

    @pytest.mark.parametrize(
        "key",
        [
            "",
            "test-1",          # lowercase
            "TEST",            # no dash
            "TEST-",           # no number
            "-1",              # no project
            "TEST-abc",        # non-numeric
            "../etc/passwd",
            "TEST/1",
            "TEST-1\x00",
            "TEST-1\r\n",
            "1-TEST",          # starts with digit
            "TEST-1.json",
            "ABC_DEF-1",       # underscore — Atlassian rejects, so do we
            "А-1",             # Cyrillic А (looks like Latin A)
            "A" * 100 + "-1",  # absurd project length
            "A-" + "9" * 20,   # absurd issue number length
            None,
            123,
            ["TEST-1"],
        ],
    )
    def test_invalid(self, key):
        assert is_valid_issue_key(key) is False


class TestSafeJoinUnder:
    def test_normal_join(self, tmp_path):
        result = safe_join_under(tmp_path, "issues", "TEST-1.json")
        assert result == (tmp_path / "issues" / "TEST-1.json").resolve()

    def test_traversal_blocked(self, tmp_path):
        with pytest.raises(ValueError, match="Path traversal"):
            safe_join_under(tmp_path, "..", "evil")

    def test_nested_traversal_blocked(self, tmp_path):
        with pytest.raises(ValueError, match="Path traversal"):
            safe_join_under(tmp_path, "issues", "..", "..", "etc", "passwd")

    def test_absolute_path_blocked(self, tmp_path):
        with pytest.raises(ValueError, match="Path traversal"):
            safe_join_under(tmp_path, "/etc/passwd")

    def test_symlink_escape_blocked(self, tmp_path):
        # Create a symlink inside base that points outside.
        outside = tmp_path.parent / "outside_target"
        outside.mkdir(exist_ok=True)
        link = tmp_path / "escape"
        link.symlink_to(outside)
        with pytest.raises(ValueError, match="Path traversal"):
            safe_join_under(tmp_path, "escape", "x.json")
