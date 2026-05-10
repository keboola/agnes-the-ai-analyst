"""Session queue and uploaded-log management for `agnes push`.

The push command operates on a queue file
(``<workspace>/.claude/agnes-sessions.txt``) populated by the
``agnes capture-session`` SessionStart hook. Each line is a TSV pair:
``<session_id>\\t<transcript_path>``. session_id is needed so the
push and slash-command machinery can filter against the private
list (``cli/lib/private_list.py``).

Backward compatibility: legacy lines without a tab (just an absolute
path) are accepted and treated as having an empty session_id. They
still upload via push but cannot be marked private retroactively —
which is fine, since by definition they pre-date the feature.

Race protection: push atomically renames the queue to a snapshot file
before processing. New SessionStart hooks write to a freshly-created
queue without their entries being clobbered by the eventual rewrite.

Recovery: if push crashes mid-snapshot, the snapshot file persists. The
next push picks it up via :func:`find_recovery_snapshots` and processes
it before touching the live queue.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path


_QUEUE_FILENAME = "agnes-sessions.txt"
_UPLOADED_FILENAME = "agnes-sessions-uploaded.txt"
_PRIVATE_SKIPPED_FILENAME = "agnes-sessions-private-skipped.txt"
_SNAPSHOT_PREFIX = "agnes-sessions.snapshot."
_SNAPSHOT_SUFFIX = ".txt"


def _claude_dir(workspace: Path) -> Path:
    """Return ``<workspace>/.claude``, creating it if missing."""
    d = workspace / ".claude"
    d.mkdir(parents=True, exist_ok=True)
    return d


def queue_path(workspace: Path) -> Path:
    return _claude_dir(workspace) / _QUEUE_FILENAME


def uploaded_log_path(workspace: Path) -> Path:
    return _claude_dir(workspace) / _UPLOADED_FILENAME


def private_skipped_log_path(workspace: Path) -> Path:
    return _claude_dir(workspace) / _PRIVATE_SKIPPED_FILENAME


def append_to_queue(workspace: Path, session_id: str, transcript_path: str) -> None:
    """Append a ``<session_id>\\t<transcript_path>`` line to the queue.

    Single-line append in O_APPEND mode — atomic for sub-PIPE_BUF writes
    on POSIX, atomic for sub-512-byte writes on NTFS. No deduplication
    here: the queue may legitimately contain duplicates (e.g., resume
    scenario re-writes the same path). Dedup happens at read time.
    """
    sid = (session_id or "").rstrip("\n").rstrip("\t")
    tp = transcript_path.rstrip("\n")
    line = f"{sid}\t{tp}\n"
    with open(queue_path(workspace), "a", encoding="utf-8") as f:
        f.write(line)


def snapshot_queue(workspace: Path) -> Path | None:
    """Atomically rename the live queue to a snapshot for processing.

    Returns the snapshot path, or None if the queue doesn't exist (no work
    to do). The snapshot filename embeds the current PID so concurrent push
    runs — which the lock already prevents, but defense-in-depth — wouldn't
    collide on the rename.
    """
    queue = queue_path(workspace)
    if not queue.exists():
        return None
    snapshot = _claude_dir(workspace) / f"{_SNAPSHOT_PREFIX}{os.getpid()}{_SNAPSHOT_SUFFIX}"
    try:
        os.rename(queue, snapshot)
    except FileNotFoundError:
        return None  # race: queue removed between exists() and rename()
    return snapshot


def _parse_queue_line(raw: str) -> tuple[str, Path] | None:
    """Parse one queue line into (session_id, path), or None if blank/invalid."""
    s = raw.strip()
    if not s:
        return None
    if "\t" in s:
        sid, _, p = s.partition("\t")
        sid = sid.strip()
        p = p.strip()
    else:
        # Legacy format: bare path, no session_id known.
        sid = ""
        p = s
    if not p:
        return None
    return sid, Path(p)


def read_entries_from_snapshot(snapshot: Path) -> list[tuple[str, Path]]:
    """Read (session_id, path) entries from a snapshot, deduplicated.

    Deduplication is by the (session_id, path) pair — preserves first-seen
    order. Blank lines and lines without a path are skipped. Mixed legacy
    (1-column) and new (2-column) lines coexist.

    Repeats from the resume scenario collapse into a single entry: the
    server-side overwrite makes a second upload of the same path redundant
    within one push run.
    """
    if not snapshot.exists():
        return []
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, Path]] = []
    for raw in snapshot.read_text(encoding="utf-8").splitlines():
        parsed = _parse_queue_line(raw)
        if parsed is None:
            continue
        sid, path = parsed
        key = (sid, str(path))
        if key in seen:
            continue
        seen.add(key)
        out.append(parsed)
    return out


# Backward-compatible alias for code that only needs paths. Returns just
# the paths (preserving the old ``list[Path]`` shape) for callers that
# don't care about session_id. Internally used by the dry-run preview
# path which only displays files.
def read_paths_from_snapshot(snapshot: Path) -> list[Path]:
    return [path for _sid, path in read_entries_from_snapshot(snapshot)]


def find_recovery_snapshots(workspace: Path) -> list[Path]:
    """Return any pre-existing snapshot files left behind by a crashed push."""
    return sorted(_claude_dir(workspace).glob(f"{_SNAPSHOT_PREFIX}*{_SNAPSHOT_SUFFIX}"))


def discard_snapshot(snapshot: Path) -> None:
    """Delete a fully-processed snapshot file. Idempotent."""
    try:
        snapshot.unlink()
    except FileNotFoundError:
        pass


def mark_uploaded(
    workspace: Path,
    transcript_path: Path,
    when: datetime | None = None,
) -> None:
    """Append `<iso_timestamp>\\t<absolute_path>\\n` to the uploaded log."""
    if when is None:
        when = datetime.now(timezone.utc)
    ts = when.strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"{ts}\t{transcript_path}\n"
    with open(uploaded_log_path(workspace), "a", encoding="utf-8") as f:
        f.write(line)


def mark_private_skipped(
    workspace: Path,
    session_id: str,
    transcript_path: Path,
    when: datetime | None = None,
) -> None:
    """Append `<iso_timestamp>\\t<session_id>\\t<path>` to the private-skipped audit log.

    Called by push when it filters out an entry whose session_id is on
    the private list. The audit log is append-only — its purpose is to
    surface (during incident review or user support) which sessions were
    intentionally NOT uploaded.
    """
    if when is None:
        when = datetime.now(timezone.utc)
    ts = when.strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"{ts}\t{session_id}\t{transcript_path}\n"
    with open(private_skipped_log_path(workspace), "a", encoding="utf-8") as f:
        f.write(line)


def requeue_failed(
    workspace: Path,
    entries: list[tuple[str, Path]],
) -> None:
    """Append failed (session_id, path) entries back to the live queue.

    Failed entries land at the end of the queue alongside any fresh
    appends that hooks wrote during this push run. Relative ordering
    vs. those fresh entries is best-effort — order doesn't affect
    correctness.
    """
    if not entries:
        return
    with open(queue_path(workspace), "a", encoding="utf-8") as f:
        for sid, p in entries:
            f.write(f"{sid}\t{p}\n")
