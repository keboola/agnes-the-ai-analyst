"""Tests for cli.lib.session_queue — queue and uploaded-log helpers."""

from datetime import datetime, timezone
from pathlib import Path

from cli.lib.session_queue import (
    append_to_queue,
    discard_snapshot,
    failed_log_path,
    find_recovery_snapshots,
    mark_failed_permanent,
    mark_private_skipped,
    mark_uploaded,
    private_skipped_log_path,
    queue_path,
    read_entries_from_snapshot,
    read_paths_from_snapshot,
    requeue_failed,
    snapshot_queue,
    uploaded_log_path,
)


def test_append_creates_claude_dir_and_appends(tmp_path):
    append_to_queue(tmp_path, "sid-1", "/abc/123.jsonl")
    queue = queue_path(tmp_path)
    assert queue.exists()
    assert queue.read_text(encoding="utf-8") == "sid-1\t/abc/123.jsonl\n"

    append_to_queue(tmp_path, "sid-2", "/def/456.jsonl")
    assert queue.read_text(encoding="utf-8") == (
        "sid-1\t/abc/123.jsonl\n"
        "sid-2\t/def/456.jsonl\n"
    )


def test_append_strips_trailing_newline_from_input(tmp_path):
    """Defensive: appender adds exactly one '\\n' even if caller passes one."""
    append_to_queue(tmp_path, "sid-1", "/abc.jsonl\n")
    assert queue_path(tmp_path).read_text(encoding="utf-8") == "sid-1\t/abc.jsonl\n"


def test_append_empty_session_id_writes_leading_tab(tmp_path):
    """Empty session_id is legal — line starts with the tab separator.
    Used by code paths where session_id isn't yet known (none today, but
    keeps the door open for backfill tooling)."""
    append_to_queue(tmp_path, "", "/abc.jsonl")
    assert queue_path(tmp_path).read_text(encoding="utf-8") == "\t/abc.jsonl\n"


def test_snapshot_returns_none_when_no_queue(tmp_path):
    assert snapshot_queue(tmp_path) is None


def test_snapshot_renames_queue_atomically(tmp_path):
    append_to_queue(tmp_path, "sid-1", "/abc.jsonl")
    snap = snapshot_queue(tmp_path)
    assert snap is not None
    assert snap.exists()
    assert not queue_path(tmp_path).exists()
    assert snap.read_text(encoding="utf-8") == "sid-1\t/abc.jsonl\n"


def test_snapshot_filename_carries_pid(tmp_path):
    import os
    append_to_queue(tmp_path, "sid", "/x.jsonl")
    snap = snapshot_queue(tmp_path)
    assert snap is not None
    assert str(os.getpid()) in snap.name


def test_snapshot_filename_is_unique_per_call(tmp_path):
    """Two consecutive snapshots under the same PID must not collide.
    Guards against the data-loss scenario where a crashed push left a
    snapshot on disk and the OS later reused its PID for a new push:
    without the uuid suffix, os.rename would atomically overwrite the
    recovery snapshot, silently losing its entries."""
    append_to_queue(tmp_path, "sid-1", "/a.jsonl")
    snap1 = snapshot_queue(tmp_path)
    append_to_queue(tmp_path, "sid-2", "/b.jsonl")
    snap2 = snapshot_queue(tmp_path)
    assert snap1 is not None and snap2 is not None
    assert snap1 != snap2
    assert snap1.exists() and snap2.exists()


def test_read_entries_dedups_and_preserves_order(tmp_path):
    append_to_queue(tmp_path, "sid-1", "/abc.jsonl")
    append_to_queue(tmp_path, "sid-2", "/def.jsonl")
    append_to_queue(tmp_path, "sid-1", "/abc.jsonl")  # exact duplicate
    snap = snapshot_queue(tmp_path)
    assert snap is not None
    entries = read_entries_from_snapshot(snap)
    assert entries == [("sid-1", Path("/abc.jsonl")), ("sid-2", Path("/def.jsonl"))]


def test_read_entries_different_session_id_same_path_kept(tmp_path):
    """Same path with different session IDs are distinct entries — they
    represent two sessions that wrote to the same transcript file (rare
    but possible if Claude Code renames sessions)."""
    append_to_queue(tmp_path, "sid-1", "/abc.jsonl")
    append_to_queue(tmp_path, "sid-2", "/abc.jsonl")
    snap = snapshot_queue(tmp_path)
    entries = read_entries_from_snapshot(snap)
    assert len(entries) == 2


def test_read_entries_accepts_legacy_one_column_lines(tmp_path):
    """Forward compat: pre-feature workspaces had bare-path lines. Those
    still upload, just with empty session_id (can't be marked private
    retroactively, which is fine — they pre-date the feature)."""
    queue = queue_path(tmp_path)
    queue.write_text("/legacy.jsonl\nsid-1\t/new.jsonl\n", encoding="utf-8")
    entries = read_entries_from_snapshot(queue)
    assert entries == [("", Path("/legacy.jsonl")), ("sid-1", Path("/new.jsonl"))]


def test_read_entries_skips_blank_lines(tmp_path):
    queue = queue_path(tmp_path)
    queue.write_text("sid-1\t/abc.jsonl\n\n  \nsid-2\t/def.jsonl\n", encoding="utf-8")
    entries = read_entries_from_snapshot(queue)
    assert entries == [("sid-1", Path("/abc.jsonl")), ("sid-2", Path("/def.jsonl"))]


def test_read_entries_returns_empty_for_missing_file(tmp_path):
    assert read_entries_from_snapshot(tmp_path / "does-not-exist.txt") == []


def test_read_paths_compat_wrapper(tmp_path):
    """read_paths_from_snapshot returns list[Path] only — used by code
    paths (dry-run preview) that don't need session_id."""
    append_to_queue(tmp_path, "sid-1", "/abc.jsonl")
    append_to_queue(tmp_path, "sid-2", "/def.jsonl")
    snap = snapshot_queue(tmp_path)
    paths = read_paths_from_snapshot(snap)
    assert paths == [Path("/abc.jsonl"), Path("/def.jsonl")]


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
    assert line.startswith("20")
    assert line.split("\t")[0].endswith("Z")


def test_mark_private_skipped_appends_audit_log(tmp_path):
    when = datetime(2026, 5, 10, 14, 32, 18, tzinfo=timezone.utc)
    p = tmp_path / "abc.jsonl"
    mark_private_skipped(tmp_path, "sid-1", p, when=when)
    log = private_skipped_log_path(tmp_path)
    assert log.read_text(encoding="utf-8") == f"2026-05-10T14:32:18Z\tsid-1\t{p}\n"


def test_mark_failed_permanent_appends_tsv(tmp_path):
    when = datetime(2026, 5, 10, 14, 32, 18, tzinfo=timezone.utc)
    p = tmp_path / "abc.jsonl"
    mark_failed_permanent(tmp_path, "sid-1", p, 401, when=when)
    log = failed_log_path(tmp_path)
    assert log.read_text(encoding="utf-8") == f"2026-05-10T14:32:18Z\tsid-1\t401\t{p}\n"

    when2 = datetime(2026, 5, 10, 14, 32, 19, tzinfo=timezone.utc)
    p2 = tmp_path / "def.jsonl"
    mark_failed_permanent(tmp_path, "sid-2", p2, 413, when=when2)
    assert log.read_text(encoding="utf-8") == (
        f"2026-05-10T14:32:18Z\tsid-1\t401\t{p}\n"
        f"2026-05-10T14:32:19Z\tsid-2\t413\t{p2}\n"
    )


def test_requeue_failed_appends_to_live_queue(tmp_path):
    fresh = tmp_path / "fresh.jsonl"
    failed = tmp_path / "failed.jsonl"
    append_to_queue(tmp_path, "sid-fresh", str(fresh))
    requeue_failed(tmp_path, [("sid-failed", failed)])
    assert queue_path(tmp_path).read_text(encoding="utf-8") == (
        f"sid-fresh\t{fresh}\n"
        f"sid-failed\t{failed}\n"
    )


def test_requeue_empty_is_noop(tmp_path):
    requeue_failed(tmp_path, [])
    assert not queue_path(tmp_path).exists()


def test_find_recovery_snapshots_returns_existing(tmp_path):
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "agnes-sessions.snapshot.111.txt").write_text("sid-a\t/a.jsonl\n")
    (claude / "agnes-sessions.snapshot.222.txt").write_text("sid-b\t/b.jsonl\n")
    snaps = find_recovery_snapshots(tmp_path)
    assert len(snaps) == 2
    assert all(s.name.startswith("agnes-sessions.snapshot.") for s in snaps)


def test_find_recovery_snapshots_empty_when_none(tmp_path):
    assert find_recovery_snapshots(tmp_path) == []


def test_discard_snapshot_idempotent(tmp_path):
    append_to_queue(tmp_path, "sid", "/a.jsonl")
    snap = snapshot_queue(tmp_path)
    assert snap is not None
    discard_snapshot(snap)
    assert not snap.exists()
    discard_snapshot(snap)  # second call must not raise


def test_append_concurrent_threads_no_corruption(tmp_path):
    """Concurrent appends from multiple threads must not interleave bytes
    mid-line. Guards against the Windows non-atomic open(path, 'a')
    regression that the queue lock was added to prevent."""
    import threading

    per_worker = 50
    n_workers = 4

    def worker(start: int) -> None:
        for i in range(per_worker):
            append_to_queue(tmp_path, f"sid-{start}-{i}", f"/p/{start}-{i}.jsonl")

    threads = [threading.Thread(target=worker, args=(s,)) for s in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    text = queue_path(tmp_path).read_text(encoding="utf-8")
    lines = text.splitlines()
    assert len(lines) == n_workers * per_worker
    # Every line must be well-formed: exactly one tab separator, non-empty
    # sid + non-empty path. Interleaved bytes would produce malformed lines.
    seen: set[str] = set()
    for line in lines:
        assert line.count("\t") == 1, f"corrupted line: {line!r}"
        sid, _, path = line.partition("\t")
        assert sid.startswith("sid-")
        assert path.startswith("/p/")
        seen.add(line)
    assert len(seen) == n_workers * per_worker  # no dropped writes
