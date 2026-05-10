"""Tests for cli.lib.session_queue — queue and uploaded-log helpers."""

from datetime import datetime, timezone
from pathlib import Path

from cli.lib.session_queue import (
    append_to_queue,
    discard_snapshot,
    find_recovery_snapshots,
    mark_uploaded,
    queue_path,
    read_paths_from_snapshot,
    requeue_failed,
    snapshot_queue,
    uploaded_log_path,
)


def test_append_creates_claude_dir_and_appends(tmp_path):
    append_to_queue(tmp_path, "/abc/123.jsonl")
    queue = queue_path(tmp_path)
    assert queue.exists()
    assert queue.read_text(encoding="utf-8") == "/abc/123.jsonl\n"

    append_to_queue(tmp_path, "/def/456.jsonl")
    assert queue.read_text(encoding="utf-8") == "/abc/123.jsonl\n/def/456.jsonl\n"


def test_append_strips_trailing_newline_from_input(tmp_path):
    """Defensive: appender adds exactly one '\\n' even if caller passes one."""
    append_to_queue(tmp_path, "/abc.jsonl\n")
    assert queue_path(tmp_path).read_text(encoding="utf-8") == "/abc.jsonl\n"


def test_snapshot_returns_none_when_no_queue(tmp_path):
    assert snapshot_queue(tmp_path) is None


def test_snapshot_renames_queue_atomically(tmp_path):
    append_to_queue(tmp_path, "/abc.jsonl")
    snap = snapshot_queue(tmp_path)
    assert snap is not None
    assert snap.exists()
    assert not queue_path(tmp_path).exists()
    assert snap.read_text(encoding="utf-8") == "/abc.jsonl\n"


def test_snapshot_filename_carries_pid(tmp_path):
    import os
    append_to_queue(tmp_path, "/x.jsonl")
    snap = snapshot_queue(tmp_path)
    assert snap is not None
    assert str(os.getpid()) in snap.name


def test_read_paths_dedups_and_preserves_order(tmp_path):
    append_to_queue(tmp_path, "/abc.jsonl")
    append_to_queue(tmp_path, "/def.jsonl")
    append_to_queue(tmp_path, "/abc.jsonl")  # duplicate
    snap = snapshot_queue(tmp_path)
    assert snap is not None
    paths = read_paths_from_snapshot(snap)
    assert paths == [Path("/abc.jsonl"), Path("/def.jsonl")]


def test_read_paths_skips_blank_lines(tmp_path):
    queue = queue_path(tmp_path)
    queue.write_text("/abc.jsonl\n\n  \n/def.jsonl\n", encoding="utf-8")
    paths = read_paths_from_snapshot(queue)
    assert paths == [Path("/abc.jsonl"), Path("/def.jsonl")]


def test_read_paths_returns_empty_for_missing_file(tmp_path):
    assert read_paths_from_snapshot(tmp_path / "does-not-exist.txt") == []


def test_mark_uploaded_appends_tsv(tmp_path):
    when = datetime(2026, 5, 10, 14, 32, 18, tzinfo=timezone.utc)
    p1 = tmp_path / "abc.jsonl"
    mark_uploaded(tmp_path, p1, when=when)
    log = uploaded_log_path(tmp_path)
    assert log.read_text(encoding="utf-8") == f"2026-05-10T14:32:18Z\t{p1}\n"

    when2 = datetime(2026, 5, 10, 14, 32, 19, tzinfo=timezone.utc)
    p2 = tmp_path / "def.jsonl"
    mark_uploaded(tmp_path, p2, when=when2)
    assert log.read_text(encoding="utf-8") == (
        f"2026-05-10T14:32:18Z\t{p1}\n"
        f"2026-05-10T14:32:19Z\t{p2}\n"
    )


def test_mark_uploaded_default_timestamp_is_utc(tmp_path):
    p = tmp_path / "x.jsonl"
    mark_uploaded(tmp_path, p)
    line = uploaded_log_path(tmp_path).read_text(encoding="utf-8")
    assert line.endswith(f"\t{p}\n")
    assert line.startswith("20")  # year prefix
    assert line.split("\t")[0].endswith("Z")  # UTC suffix


def test_requeue_failed_appends_to_live_queue(tmp_path):
    fresh = tmp_path / "fresh.jsonl"
    failed = tmp_path / "failed.jsonl"
    append_to_queue(tmp_path, str(fresh))  # simulate hook write
    requeue_failed(tmp_path, [failed])
    assert queue_path(tmp_path).read_text(encoding="utf-8") == f"{fresh}\n{failed}\n"


def test_requeue_empty_is_noop(tmp_path):
    requeue_failed(tmp_path, [])
    assert not queue_path(tmp_path).exists()


def test_find_recovery_snapshots_returns_existing(tmp_path):
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "agnes-sessions.snapshot.111.txt").write_text("/a.jsonl\n")
    (claude / "agnes-sessions.snapshot.222.txt").write_text("/b.jsonl\n")
    snaps = find_recovery_snapshots(tmp_path)
    assert len(snaps) == 2
    assert all(s.name.startswith("agnes-sessions.snapshot.") for s in snaps)


def test_find_recovery_snapshots_empty_when_none(tmp_path):
    assert find_recovery_snapshots(tmp_path) == []


def test_discard_snapshot_idempotent(tmp_path):
    append_to_queue(tmp_path, "/a.jsonl")
    snap = snapshot_queue(tmp_path)
    assert snap is not None
    discard_snapshot(snap)
    assert not snap.exists()
    discard_snapshot(snap)  # second call must not raise
