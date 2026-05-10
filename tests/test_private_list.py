"""Tests for cli.lib.private_list — authoritative "do not upload" state."""

from cli.lib.private_list import (
    add_private,
    is_private,
    private_list_path,
    read_all_private,
)


def test_is_private_false_when_file_missing(tmp_path):
    assert is_private(tmp_path, "abc") is False


def test_is_private_false_for_empty_session_id(tmp_path):
    add_private(tmp_path, "abc")
    assert is_private(tmp_path, "") is False


def test_add_private_creates_claude_dir_and_writes(tmp_path):
    assert add_private(tmp_path, "abc") is True
    assert private_list_path(tmp_path).exists()
    assert is_private(tmp_path, "abc") is True


def test_add_private_is_idempotent(tmp_path):
    assert add_private(tmp_path, "abc") is True
    assert add_private(tmp_path, "abc") is False  # second call: already present
    # File has exactly one line
    contents = private_list_path(tmp_path).read_text(encoding="utf-8")
    assert contents == "abc\n"


def test_add_private_empty_id_noop(tmp_path):
    assert add_private(tmp_path, "") is False
    assert not private_list_path(tmp_path).exists()


def test_read_all_private_returns_set(tmp_path):
    add_private(tmp_path, "abc")
    add_private(tmp_path, "def")
    assert read_all_private(tmp_path) == {"abc", "def"}


def test_read_all_private_skips_blank_lines(tmp_path):
    path = private_list_path(tmp_path)
    path.write_text("abc\n\n  \ndef\n", encoding="utf-8")
    assert read_all_private(tmp_path) == {"abc", "def"}


def test_read_all_private_empty_when_file_missing(tmp_path):
    assert read_all_private(tmp_path) == set()
