"""Session queue and uploaded-log management for `agnes push`.

The push command operates on a queue file
(``<workspace>/.claude/agnes-sessions.txt``) populated by the
``agnes capture-session`` SessionStart + SessionEnd hooks. Each line is
a TSV row: ``<session_id>\\t<transcript_path>[\\t<first_failed_iso>]``.
session_id is needed so the push and slash-command machinery can filter
against the private list (``cli/lib/private_list.py``).

The optional third column is a retry-bookkeeping stamp: the UTC ISO
timestamp of the FIRST failed upload attempt for this entry. Entries
whose first failure is older than :data:`RETRY_TTL` get dropped to the
permanent-failure audit log instead of being requeued — bounding the
queue without ever silently discarding a session that might still
appear (the transcript file is created lazily by Claude Code on the
first prompt, so "file not found" at push time usually means "not
written YET", not "deleted").

Backward compatibility: legacy lines without a tab (just an absolute
path) are accepted and treated as having an empty session_id. They
still upload via push but cannot be marked private retroactively —
which is fine, since by definition they pre-date the feature.
Two-column lines (pre-stamp era) parse with an empty stamp.

Race protection: push atomically renames the queue to a snapshot file
before processing. New SessionStart hooks write to a freshly-created
queue without their entries being clobbered by the eventual rewrite.
A short-lived ``agnes-queue.lock`` (filelock) serializes the rename
against in-flight appends so the queue file is never written to and
renamed concurrently — required on Windows, where ``os.rename`` fails
if another handle has the file open, and where ``open(path, "a")`` is
not atomic across writers.

Recovery: if push crashes mid-snapshot, the snapshot file persists. The
next push picks it up via :func:`find_recovery_snapshots` and processes
it before touching the live queue.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from filelock import FileLock


# How long a failing queue entry keeps being retried before push gives up
# and moves it to the permanent-failure audit log. 30 days matches Claude
# Code's default transcript retention (cleanupPeriodDays) — past that the
# jsonl is gone from disk anyway, so further retries can't succeed.
RETRY_TTL = timedelta(days=30)

_QUEUE_FILENAME = "agnes-sessions.txt"
_UPLOADED_FILENAME = "agnes-sessions-uploaded.txt"
_PRIVATE_SKIPPED_FILENAME = "agnes-sessions-private-skipped.txt"
_FAILED_FILENAME = "agnes-sessions-failed.txt"
_SNAPSHOT_PREFIX = "agnes-sessions.snapshot."
_SNAPSHOT_SUFFIX = ".txt"
_QUEUE_LOCK_FILENAME = "agnes-queue.lock"


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


def failed_log_path(workspace: Path) -> Path:
    return _claude_dir(workspace) / _FAILED_FILENAME


def _queue_lock_path(workspace: Path) -> Path:
    """Lock file serializing concurrent writers to the queue file.

    Separate from ``agnes-push.lock`` — that one serializes the push
    command end-to-end; this one is short-lived (held only for the
    duration of a single append or rename).
    """
    return _claude_dir(workspace) / _QUEUE_LOCK_FILENAME


def append_to_queue(workspace: Path, session_id: str, transcript_path: str) -> None:
    """Append a ``<session_id>\\t<transcript_path>`` line to the queue.

    Held under ``agnes-queue.lock`` to serialize concurrent SessionStart
    hooks. Python's ``open(path, "a")`` is NOT atomic on Windows — the
    CRT does not pass ``FILE_APPEND_DATA`` to ``CreateFile``, so it's a
    plain seek-to-end + write that can interleave bytes mid-line under
    concurrent writers (e.g. user opens several Claude Code windows
    simultaneously). The lock makes the append safe on every platform.

    No deduplication here: duplicates may legitimately appear (resume
    scenario re-writes the same path). Dedup happens at read time.
    """
    sid = (session_id or "").rstrip("\n").rstrip("\t")
    tp = transcript_path.rstrip("\n")
    line = f"{sid}\t{tp}\n"
    with FileLock(str(_queue_lock_path(workspace))):
        with open(queue_path(workspace), "a", encoding="utf-8") as f:
            f.write(line)


def snapshot_queue(workspace: Path) -> Path | None:
    """Atomically rename the live queue to a snapshot for processing.

    Returns the snapshot path, or None if the queue doesn't exist (no work
    to do). The snapshot filename embeds the current PID *and* a random
    uuid8 hex tail: PID alone is not unique after the OS recycles it
    (Linux wraps at ~32768 by default), so a crashed push leaving a
    snapshot on disk could be silently overwritten by a future push with
    the same PID — ``os.rename`` atomically replaces the destination on
    POSIX and Windows alike, so data loss would be silent. The uuid tail
    makes every snapshot filename unique regardless of PID reuse.

    Held under ``agnes-queue.lock`` to serialize against in-flight
    ``append_to_queue`` calls: on Windows, ``os.rename`` would fail with
    ``PermissionError`` if another handle has the queue open for write,
    so the lock prevents that race. The lock is short-lived (single
    rename), so it doesn't meaningfully delay concurrent capture-session
    hooks.
    """
    queue = queue_path(workspace)
    if not queue.exists():
        return None
    unique = uuid.uuid4().hex[:8]
    snapshot = _claude_dir(workspace) / f"{_SNAPSHOT_PREFIX}{os.getpid()}.{unique}{_SNAPSHOT_SUFFIX}"
    with FileLock(str(_queue_lock_path(workspace))):
        try:
            os.rename(queue, snapshot)
        except FileNotFoundError:
            return None  # race: queue removed between exists() and rename()
    return snapshot


def _parse_queue_line(raw: str) -> tuple[str, Path, str] | None:
    """Parse one queue line into (session_id, path, first_failed_iso),
    or None if blank/invalid. ``first_failed_iso`` is "" for lines that
    never failed (2-column) and for legacy bare-path lines (1-column).
    """
    s = raw.strip()
    if not s:
        return None
    if "\t" in s:
        sid, _, rest = s.partition("\t")
        sid = sid.strip()
        p, _, stamp = rest.partition("\t")
        p = p.strip()
        stamp = stamp.strip()
    else:
        # Legacy format: bare path, no session_id known.
        sid = ""
        p = s
        stamp = ""
    if not p:
        return None
    return sid, Path(p), stamp


def retry_expired(first_failed_iso: str, now: datetime | None = None) -> bool:
    """True iff a failure stamp exists and is older than :data:`RETRY_TTL`.

    An empty or unparsable stamp returns False — the entry keeps being
    retried (and push re-stamps it), which fails safe: we never drop a
    session because of a corrupt bookkeeping column.
    """
    if not first_failed_iso:
        return False
    try:
        first = datetime.strptime(first_failed_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    if now is None:
        now = datetime.now(timezone.utc)
    return now - first > RETRY_TTL


def read_entries_from_snapshot(snapshot: Path) -> list[tuple[str, Path, str]]:
    """Read (session_id, path, first_failed_iso) entries, deduplicated.

    Deduplication is by the (session_id, path) pair — preserves first-seen
    order (and therefore the first-seen failure stamp, keeping the TTL
    anchored at the ORIGINAL failure even when an entry got requeued and
    re-captured). Blank lines and lines without a path are skipped. Mixed
    legacy (1-column), pre-stamp (2-column) and stamped (3-column) lines
    coexist.

    Repeats from the resume scenario collapse into a single entry: the
    server-side overwrite makes a second upload of the same path redundant
    within one push run.
    """
    if not snapshot.exists():
        return []
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, Path, str]] = []
    for raw in snapshot.read_text(encoding="utf-8").splitlines():
        parsed = _parse_queue_line(raw)
        if parsed is None:
            continue
        sid, path, _stamp = parsed
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
    return [path for _sid, path, _stamp in read_entries_from_snapshot(snapshot)]


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


def mark_failed_permanent(
    workspace: Path,
    session_id: str,
    transcript_path: Path,
    status_code: int | str,
    when: datetime | None = None,
) -> None:
    """Append `<iso_timestamp>\\t<session_id>\\t<status>\\t<path>` to the
    permanent-failure audit log.

    Called by push when the server returns a 4xx other than 401 / 408 /
    429 — deterministic failures where retrying never succeeds (403 RBAC
    denial, 413 payload too large, 400 server validation, etc.) — and
    when a retried entry's first failure is older than :data:`RETRY_TTL`
    (``status_code`` then carries a reason string such as
    ``not_found_expired`` instead of an HTTP status). 401 is transient:
    re-auth makes the same upload succeed, so it requeues until the TTL.
    The transcript path is logged here instead of silently dropped so
    operators have a forensic trail; the entry is NOT re-queued,
    breaking the prior infinite-loop bug where every push run would
    re-bombard the server with the same failing upload.

    No separate lock: piggybacks on `agnes-push.lock` (the
    single-instance push lock), same as `mark_uploaded` and
    `mark_private_skipped`. Push is the only writer to this file.
    """
    if when is None:
        when = datetime.now(timezone.utc)
    ts = when.strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"{ts}\t{session_id}\t{status_code}\t{transcript_path}\n"
    with open(failed_log_path(workspace), "a", encoding="utf-8") as f:
        f.write(line)


def requeue_failed(
    workspace: Path,
    entries: list[tuple[str, Path, str]],
) -> None:
    """Append failed (session_id, path, first_failed_iso) entries back to
    the live queue.

    The third column is the retry-bookkeeping stamp (UTC ISO of the FIRST
    failure); push fills it in before requeueing so :func:`retry_expired`
    can bound the retry window. An empty stamp writes a 2-column line —
    identical to a fresh capture.

    Failed entries land at the end of the queue alongside any fresh
    appends that hooks wrote during this push run. Relative ordering
    vs. those fresh entries is best-effort — order doesn't affect
    correctness.

    Held under ``agnes-queue.lock`` because concurrent ``capture-session``
    hooks (which don't hold the push lock) may be appending at the same
    time — same Windows non-atomicity concern as ``append_to_queue``.
    """
    if not entries:
        return
    with FileLock(str(_queue_lock_path(workspace))):
        with open(queue_path(workspace), "a", encoding="utf-8") as f:
            for sid, p, stamp in entries:
                if stamp:
                    f.write(f"{sid}\t{p}\t{stamp}\n")
                else:
                    f.write(f"{sid}\t{p}\n")
