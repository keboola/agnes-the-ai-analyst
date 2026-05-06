"""Unit tests for src.store_naming."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.store_naming import (
    compute_entity_version,
    sanitize_username,
    suffixed_name,
)


class TestSanitizeUsername:
    @pytest.mark.parametrize("email,expected", [
        ("alice_smith@example.com", "alice-smith"),
        ("john.doe+claude@acme.com", "john-doe-claude"),
        ("USER@example.com", "user"),
        ("a.b.c@x.y", "a-b-c"),
        ("plain@example.com", "plain"),
        ("UPPER_CASE-name@example.com", "upper-case-name"),
        ("name123@example.com", "name123"),
        ("dots..multiple..@example.com", "dots-multiple"),
    ])
    def test_known_inputs(self, email, expected):
        assert sanitize_username(email) == expected

    def test_empty_after_sanitize_raises(self):
        with pytest.raises(ValueError):
            sanitize_username("---@example.com")
        with pytest.raises(ValueError):
            sanitize_username("@example.com")

    def test_strips_leading_trailing_dashes(self):
        assert sanitize_username("-foo-@example.com") == "foo"


class TestSuffixedName:
    def test_basic(self):
        assert suffixed_name("code-review", "honza") == "code-review-by-honza"

    def test_preserves_original_chars(self):
        assert suffixed_name("a-b-c", "u-v") == "a-b-c-by-u-v"


class TestComputeEntityVersion:
    def test_deterministic_same_content(self, tmp_path: Path):
        a = tmp_path / "a"; a.mkdir()
        (a / "x.txt").write_text("hello")
        (a / "y.md").write_text("---\nname: x\n---")
        v1 = compute_entity_version(a)

        b = tmp_path / "b"; b.mkdir()
        (b / "x.txt").write_text("hello")
        (b / "y.md").write_text("---\nname: x\n---")
        v2 = compute_entity_version(b)

        assert v1 == v2
        assert len(v1) == 16

    def test_changes_on_content_change(self, tmp_path: Path):
        d = tmp_path / "d"; d.mkdir()
        (d / "x.txt").write_text("hello")
        v1 = compute_entity_version(d)
        (d / "x.txt").write_text("hello!")
        v2 = compute_entity_version(d)
        assert v1 != v2

    def test_changes_on_filename_change(self, tmp_path: Path):
        d = tmp_path / "d"; d.mkdir()
        (d / "x.txt").write_text("same content")
        v1 = compute_entity_version(d)
        (d / "x.txt").rename(d / "y.txt")
        v2 = compute_entity_version(d)
        assert v1 != v2

    def test_empty_dir_returns_stable_hash(self, tmp_path: Path):
        d = tmp_path / "d"; d.mkdir()
        v = compute_entity_version(d)
        assert isinstance(v, str)
        assert len(v) == 16
