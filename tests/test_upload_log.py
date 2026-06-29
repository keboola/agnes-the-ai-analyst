"""Tests for cli/lib/upload_log.py — the upload ledger + audit-log helpers."""

from __future__ import annotations

from datetime import datetime, timezone

from cli.lib.upload_log import (
    failed_log_path,
    mark_failed_permanent,
    mark_private_skipped,
    mark_uploaded,
    private_skipped_log_path,
    read_failed_sessions,
    read_private_skipped_sessions,
    read_uploaded,
    uploaded_log_path,
)


def test_mark_uploaded_writes_sid_size_iso(tmp_path):
    when = datetime(2026, 6, 24, 12, 0, 0, tzinfo=timezone.utc)
    mark_uploaded(tmp_path, "sid-1", 123, when)
    line = uploaded_log_path(tmp_path).read_text(encoding="utf-8").strip()
    assert line == "sid-1\t123\t2026-06-24T12:00:00Z"


def test_read_uploaded_returns_sid_to_size(tmp_path):
    mark_uploaded(tmp_path, "a", 10)
    mark_uploaded(tmp_path, "b", 20)
    assert read_uploaded(tmp_path) == {"a": 10, "b": 20}


def test_read_uploaded_missing_file_is_empty(tmp_path):
    assert read_uploaded(tmp_path) == {}


def test_read_uploaded_largest_size_wins(tmp_path):
    """A re-uploaded (grown) transcript writes a second row; the larger size
    is the authoritative 'already uploaded at' size."""
    mark_uploaded(tmp_path, "a", 10)
    mark_uploaded(tmp_path, "a", 50)
    assert read_uploaded(tmp_path) == {"a": 50}
    # Order independence: a smaller later row must not shrink it.
    mark_uploaded(tmp_path, "a", 30)
    assert read_uploaded(tmp_path) == {"a": 50}


def test_read_uploaded_skips_legacy_iso_path_rows(tmp_path):
    """Legacy ``<iso>\\t<path>`` rows (non-integer 2nd field) are ignored, so
    the matching session re-uploads once under the new ledger."""
    log = uploaded_log_path(tmp_path)
    log.write_text(
        "2026-05-01T00:00:00Z\t/home/me/.claude/projects/x/abc.jsonl\n"
        "sid-new\t42\t2026-06-24T12:00:00Z\n"
        "\n"
        "garbage-no-tab\n",
        encoding="utf-8",
    )
    assert read_uploaded(tmp_path) == {"sid-new": 42}


def test_mark_private_skipped_format(tmp_path):
    when = datetime(2026, 6, 24, 9, 30, 0, tzinfo=timezone.utc)
    mark_private_skipped(tmp_path, "sid-priv", "/p/abc.jsonl", when)
    line = private_skipped_log_path(tmp_path).read_text(encoding="utf-8").strip()
    assert line == "2026-06-24T09:30:00Z\tsid-priv\t/p/abc.jsonl"


def test_mark_failed_permanent_format(tmp_path):
    when = datetime(2026, 6, 24, 9, 30, 0, tzinfo=timezone.utc)
    mark_failed_permanent(tmp_path, "sid-x", "/p/abc.jsonl", 403, when)
    line = failed_log_path(tmp_path).read_text(encoding="utf-8").strip()
    assert line == "2026-06-24T09:30:00Z\tsid-x\t403\t/p/abc.jsonl"


def test_read_failed_sessions(tmp_path):
    assert read_failed_sessions(tmp_path) == set()
    mark_failed_permanent(tmp_path, "sid-a", "/p/a.jsonl", 413)
    mark_failed_permanent(tmp_path, "sid-b", "/p/b.jsonl", 400)
    assert read_failed_sessions(tmp_path) == {"sid-a", "sid-b"}


def test_read_private_skipped_sessions(tmp_path):
    assert read_private_skipped_sessions(tmp_path) == set()
    mark_private_skipped(tmp_path, "sid-1", "/p/1.jsonl")
    mark_private_skipped(tmp_path, "sid-2", "/p/2.jsonl")
    # A duplicate row collapses in the set (the dedup primitive push relies on).
    mark_private_skipped(tmp_path, "sid-1", "/p/1.jsonl")
    assert read_private_skipped_sessions(tmp_path) == {"sid-1", "sid-2"}
