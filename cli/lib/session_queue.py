"""Session queue and uploaded-log management for `agnes push`.

The push command operates on a queue file (`<workspace>/.claude/agnes-sessions.txt`)
populated by the `agnes capture-session` SessionStart hook. Each line is the
absolute path to a Claude Code session jsonl.

Race protection: push atomically renames the queue to a snapshot file before
processing. New SessionStart hooks write to a freshly-created queue file
without their entries being clobbered by the eventual rewrite.

Recovery: if push crashes mid-snapshot, the snapshot file persists. The next
push picks it up via :func:`find_recovery_snapshots` and processes it before
touching the live queue.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path


_QUEUE_FILENAME = "agnes-sessions.txt"
_UPLOADED_FILENAME = "agnes-sessions-uploaded.txt"
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


def append_to_queue(workspace: Path, transcript_path: str) -> None:
    """Append a transcript path to the queue.

    Single-line append in O_APPEND mode — atomic for sub-PIPE_BUF writes on
    POSIX, atomic for sub-512-byte writes on NTFS. No deduplication here:
    the queue may legitimately contain duplicates (e.g., resume scenario
    re-writes the same path). Dedup happens at read time.
    """
    line = transcript_path.rstrip("\n") + "\n"
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


def read_paths_from_snapshot(snapshot: Path) -> list[Path]:
    """Read paths from a snapshot, deduplicated, preserving first-seen order.

    Empty/whitespace lines are skipped. Repeats from the resume scenario
    collapse into a single entry — the server-side overwrite makes a
    second upload of the same path redundant within one push run.
    """
    if not snapshot.exists():
        return []
    seen: set[str] = set()
    paths: list[Path] = []
    for raw in snapshot.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        if not s or s in seen:
            continue
        seen.add(s)
        paths.append(Path(s))
    return paths


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


def requeue_failed(workspace: Path, paths: list[Path]) -> None:
    """Append failed paths back to the live queue so the next push retries.

    Failed paths land at the end of the queue alongside any fresh entries
    that hooks wrote during this push run. Relative ordering vs. those
    fresh entries is best-effort — order doesn't affect correctness.
    """
    if not paths:
        return
    with open(queue_path(workspace), "a", encoding="utf-8") as f:
        for p in paths:
            f.write(f"{p}\n")
