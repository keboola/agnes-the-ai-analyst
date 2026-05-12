"""Health check: detect silently-broken `agnes capture-session`.

Issue #244. `agnes capture-session` (the SessionStart hook helper)
exits 0 on every failure mode so the hook is invisible during session
startup. If Claude Code changes its stdin contract or capture-session
crashes mid-write, the uploaded-log stops growing — but the SessionStart
events keep landing in `~/.claude/projects/<encoded>/`. The gap between
the two is a passive signal we can surface in `agnes diagnose`.

The check compares:

1. **Expected** — count of session jsonl files in every
   ``~/.claude/projects/<encoded>/`` matching the current workspace with
   ``mtime`` within the configured window.

2. **Actual** — count of entries in
   ``<workspace>/.claude/agnes-sessions-uploaded.txt`` whose
   ``<iso_timestamp>`` prefix falls within the same window.

If ``expected - actual`` exceeds the threshold, capture-session is
likely broken end-to-end. Emit a ``warning`` with both counts plus a
pointer to ``agnes capture-session --verbose`` for manual triage.

Window and threshold are conservative defaults (7d / 3) tuned to
surface stop-the-world breakage without false-positive churn on a
fresh workspace. Callers can override via ``window_days`` /
``threshold`` kwargs.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

from cli.lib.claude_sessions import find_claude_sessions_dirs
from cli.lib.session_queue import uploaded_log_path

_DEFAULT_WINDOW_DAYS = 7
_DEFAULT_THRESHOLD = 3


def _parse_uploaded_log_count(log_path: Path, cutoff: datetime) -> int:
    if not log_path.exists():
        return 0
    count = 0
    try:
        text = log_path.read_text(encoding="utf-8")
    except OSError:
        return 0
    for line in text.splitlines():
        ts_str, sep, _ = line.partition("\t")
        if not sep:
            continue
        try:
            ts = datetime.strptime(ts_str.strip(), "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue
        if ts >= cutoff:
            count += 1
    return count


def _count_recent_session_files(workspace: Path, cutoff: datetime) -> int:
    count = 0
    for d in find_claude_sessions_dirs(workspace):
        try:
            iterator = d.glob("*.jsonl")
        except OSError:
            continue
        for f in iterator:
            try:
                mtime_ts = f.stat().st_mtime
            except OSError:
                continue
            mtime = datetime.fromtimestamp(mtime_ts, tz=timezone.utc)
            if mtime >= cutoff:
                count += 1
    return count


def capture_session_health(
    workspace: Path,
    window_days: int = _DEFAULT_WINDOW_DAYS,
    threshold: int = _DEFAULT_THRESHOLD,
    now: datetime | None = None,
) -> Dict[str, Any]:
    """Return a diagnose-shaped check dict for capture-session health.

    Status values:

    - ``ok`` — expected ≈ actual within threshold.
    - ``warning`` — observed SessionStart events that capture-session
      didn't write to the uploaded log; likely broken end-to-end.
    - ``info`` — no SessionStart events in the window (no signal).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=window_days)

    expected = _count_recent_session_files(workspace, cutoff)
    actual = _parse_uploaded_log_count(uploaded_log_path(workspace), cutoff)
    delta = expected - actual

    base: Dict[str, Any] = {
        "name": "capture-session",
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
                f"{expected} SessionStart event(s) in the last {window_days}d "
                f"but only {actual} entries in agnes-sessions-uploaded.txt — "
                "capture-session may be silently failing. Try: "
                "`agnes capture-session --verbose` against a session jsonl"
            ),
        }

    return {**base, "status": "ok"}
