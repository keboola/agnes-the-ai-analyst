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
    # writable=True so the parent .claude/ exists for the manual write below.
    path = private_list_path(tmp_path, writable=True)
    path.write_text("abc\n\n  \ndef\n", encoding="utf-8")
    assert read_all_private(tmp_path) == {"abc", "def"}


def test_read_all_private_empty_when_file_missing(tmp_path):
    assert read_all_private(tmp_path) == set()


def test_read_only_path_does_not_create_claude_dir(tmp_path):
    """Hot-path reader (statusline → is_private → private_list_path)
    must NOT materialize ``.claude/`` in arbitrary directories. S2.7 from
    the PR review."""
    _ = private_list_path(tmp_path)  # default writable=False
    assert not (tmp_path / ".claude").exists()
    # Read-side helpers also stay side-effect-free.
    _ = read_all_private(tmp_path)
    assert is_private(tmp_path, "missing") is False
    assert not (tmp_path / ".claude").exists()


def test_read_cache_returns_same_set_across_calls(tmp_path):
    """mtime-keyed cache: repeated reads of an unchanged file return the
    same set (same identity is fine; same value is the contract)."""
    add_private(tmp_path, "abc")
    a = read_all_private(tmp_path)
    b = read_all_private(tmp_path)
    assert a == b == {"abc"}


def test_add_private_evicts_cache_so_subsequent_read_sees_new_id(tmp_path):
    """Within a sub-second window after add, is_private must reflect
    the new entry — even when filesystem mtime granularity is 1s and
    a naive mtime check would still treat the cache as fresh."""
    add_private(tmp_path, "first")
    assert read_all_private(tmp_path) == {"first"}
    # Second add within the same second; cache eviction makes the next
    # read see both entries even though mtime may be identical.
    add_private(tmp_path, "second")
    assert read_all_private(tmp_path) == {"first", "second"}
