"""Health check: detect sessions on disk that aren't reaching the server.

Issue #244, adapted for the scan-based `agnes push`. push uploads 0 on every
failure mode that matters here (no ``workspace_root`` anchored, the encoded
session folder doesn't resolve, the server persistently rejects uploads), so
the upload ledger stops growing while Claude Code keeps writing transcripts
into ``<projects_root>/<encoded-workspace>/``. The gap between the two is a
passive signal we surface in ``agnes diagnose``.

The check compares, within a sliding window:

1. **Expected** — count of session jsonl files in the workspace's encoded
   Claude Code folder (``cli/lib/session_paths.session_dir``) with ``mtime``
   inside the window.

2. **Actual** — count of rows in ``<workspace>/.claude/agnes-sessions-uploaded.txt``
   whose timestamp falls inside the window. The current ledger format is
   ``<session_id>\\t<size>\\t<iso_ts>``; legacy ``<iso_ts>\\t<path>`` rows are
   still counted (the parser tries the last field then the first as an ISO
   timestamp), so the check keeps working across the format change.

If ``expected - actual`` exceeds the threshold, uploads are likely broken
end-to-end. Emit a ``warning`` with both counts plus a pointer to
``agnes push --dry-run`` for manual triage.

Window and threshold are conservative defaults (7d / 3) tuned to surface
stop-the-world breakage without false-positive churn on a fresh workspace.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

from cli.lib.session_paths import session_dir
from cli.lib.upload_log import uploaded_log_path

_DEFAULT_WINDOW_DAYS = 7
_DEFAULT_THRESHOLD = 3


def _parse_iso(token: str) -> datetime | None:
    try:
        return datetime.strptime(token.strip(), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_uploaded_log_count(log_path: Path, cutoff: datetime) -> int:
    """Count ledger rows whose timestamp is within the window.

    Tolerant of both the current ``<sid>\\t<size>\\t<iso>`` format and the
    legacy ``<iso>\\t<path>`` format: the ISO timestamp is the LAST field in
    the new format and the FIRST field in the old one, so we try both.
    """
    if not log_path.exists():
        return 0
    count = 0
    try:
        text = log_path.read_text(encoding="utf-8")
    except OSError:
        return 0
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        ts = _parse_iso(parts[-1])
        if ts is None and len(parts) > 1:
            ts = _parse_iso(parts[0])
        if ts is not None and ts >= cutoff:
            count += 1
    return count


def _count_recent_session_files(workspace_root: Path, cutoff: datetime) -> int:
    d = session_dir(workspace_root)
    if not d.is_dir():
        return 0
    count = 0
    try:
        iterator = d.glob("*.jsonl")
    except OSError:
        return 0
    for f in iterator:
        try:
            mtime_ts = f.stat().st_mtime
        except OSError:
            continue
        if datetime.fromtimestamp(mtime_ts, tz=timezone.utc) >= cutoff:
            count += 1
    return count


def session_upload_health(
    workspace_root: Path,
    window_days: int = _DEFAULT_WINDOW_DAYS,
    threshold: int = _DEFAULT_THRESHOLD,
    now: datetime | None = None,
) -> Dict[str, Any]:
    """Return a diagnose-shaped check dict for session-upload health.

    Status values:

    - ``ok`` — expected ≈ actual within threshold.
    - ``warning`` — session transcripts on disk that the upload ledger
      didn't record; uploads likely broken end-to-end.
    - ``info`` — no recent sessions on disk (no signal).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=window_days)

    expected = _count_recent_session_files(workspace_root, cutoff)
    actual = _parse_uploaded_log_count(uploaded_log_path(workspace_root), cutoff)
    delta = expected - actual

    base: Dict[str, Any] = {
        "name": "session-upload",
        "expected_sessions": expected,
        "uploaded_entries": actual,
        "window_days": window_days,
    }

    if expected == 0:
        return {
            **base,
            "status": "info",
            "detail": (
                f"no Claude Code sessions observed in the last {window_days}d "
                "for this workspace — nothing to verify"
            ),
        }

    if delta > threshold:
        return {
            **base,
            "status": "warning",
            "detail": (
                f"{expected} session transcript(s) on disk in the last {window_days}d "
                f"but only {actual} entries in agnes-sessions-uploaded.txt — "
                "session upload may be failing. Try: `agnes push --dry-run`"
            ),
        }

    return {**base, "status": "ok"}
