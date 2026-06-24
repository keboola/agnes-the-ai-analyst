"""Append-only logs for `agnes push`: the upload ledger + audit trails.

Extracted from the (removed) capture/queue module when `agnes push` moved to
scanning the workspace's Claude Code session folder directly. Three files,
all under ``<workspace>/.claude/``:

- ``agnes-sessions-uploaded.txt`` — the dedup ledger. One TSV row per
  successful upload: ``<session_id>\\t<size_bytes>\\t<iso_ts>``. push reads it
  back (:func:`read_uploaded`) and skips a session whose id is present with
  the same byte size; a larger size means the transcript grew, so push
  re-uploads it (the server overwrites by filename, so re-uploading is
  idempotent). The ISO timestamp is audit-only — `read_uploaded` ignores it.

- ``agnes-sessions-private-skipped.txt`` — audit trail of sessions skipped
  because their id is on the private list (``/agnes-private``).

- ``agnes-sessions-failed.txt`` — forensic trail of permanent (4xx) upload
  failures the server will never accept.

Backward compatibility: the previous push wrote uploaded rows as
``<iso_ts>\\t<path>`` (no size). :func:`read_uploaded` parses leniently — a
row whose second field isn't an integer is treated as "not uploaded", so the
matching session re-uploads once under the new ledger (idempotent) and a
fresh new-format row supersedes it. The parser never raises on old rows.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

_UPLOADED_FILENAME = "agnes-sessions-uploaded.txt"
_PRIVATE_SKIPPED_FILENAME = "agnes-sessions-private-skipped.txt"
_FAILED_FILENAME = "agnes-sessions-failed.txt"


def _claude_dir(workspace: Path) -> Path:
    """Return ``<workspace>/.claude``, creating it if missing.

    Callers pass the workspace root (which already has a ``.claude/``), so
    the mkdir is a no-op in practice; it stays for parity with the prior
    helper and so a first push on a freshly-anchored workspace still writes.
    """
    d = workspace / ".claude"
    d.mkdir(parents=True, exist_ok=True)
    return d


def uploaded_log_path(workspace: Path) -> Path:
    return _claude_dir(workspace) / _UPLOADED_FILENAME


def private_skipped_log_path(workspace: Path) -> Path:
    return _claude_dir(workspace) / _PRIVATE_SKIPPED_FILENAME


def failed_log_path(workspace: Path) -> Path:
    return _claude_dir(workspace) / _FAILED_FILENAME


def _iso(when: datetime | None) -> str:
    return (when or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def mark_uploaded(
    workspace: Path,
    session_id: str,
    size: int,
    when: datetime | None = None,
) -> None:
    """Append ``<session_id>\\t<size>\\t<iso_ts>`` to the upload ledger."""
    line = f"{session_id}\t{int(size)}\t{_iso(when)}\n"
    with open(uploaded_log_path(workspace), "a", encoding="utf-8") as f:
        f.write(line)


def read_uploaded(workspace: Path) -> dict[str, int]:
    """Return ``{session_id: size_bytes}`` from the upload ledger.

    On duplicate session ids the LARGEST recorded size wins — a grown
    transcript that was re-uploaded supersedes its earlier shorter upload.
    Legacy ``<iso>\\t<path>`` rows (non-integer second field) and blank /
    malformed rows are skipped. Returns ``{}`` when the file is absent or
    unreadable.
    """
    path = uploaded_log_path(workspace)
    out: dict[str, int] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for raw in text.splitlines():
        parts = raw.split("\t")
        if len(parts) < 2:
            continue
        sid = parts[0].strip()
        if not sid:
            continue
        try:
            size = int(parts[1].strip())
        except ValueError:
            continue  # legacy <iso>\t<path> row, or garbage — skip
        prev = out.get(sid)
        if prev is None or size > prev:
            out[sid] = size
    return out


def mark_private_skipped(
    workspace: Path,
    session_id: str,
    transcript_path: os.PathLike | str,
    when: datetime | None = None,
) -> None:
    """Append ``<iso_ts>\\t<session_id>\\t<path>`` to the private-skipped audit log."""
    line = f"{_iso(when)}\t{session_id}\t{transcript_path}\n"
    with open(private_skipped_log_path(workspace), "a", encoding="utf-8") as f:
        f.write(line)


def mark_failed_permanent(
    workspace: Path,
    session_id: str,
    transcript_path: os.PathLike | str,
    status_code: int | str,
    when: datetime | None = None,
) -> None:
    """Append ``<iso_ts>\\t<session_id>\\t<status>\\t<path>`` to the failed audit log.

    Called when the server returns a 4xx other than 401 / 408 / 429 —
    deterministic failures retrying won't fix (403 RBAC, 413 too large, 400
    validation). The path is logged for a forensic trail rather than silently
    dropped; the session is simply not recorded as uploaded.
    """
    line = f"{_iso(when)}\t{session_id}\t{status_code}\t{transcript_path}\n"
    with open(failed_log_path(workspace), "a", encoding="utf-8") as f:
        f.write(line)
