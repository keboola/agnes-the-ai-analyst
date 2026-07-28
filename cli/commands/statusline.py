"""`agnes statusline` — Claude Code statusLine helper.

Claude Code's ``statusLine`` setting in ``settings.json`` invokes a shell
command on every status-bar render and displays the first line of its
stdout. The session payload (a JSON object containing ``session_id``,
``transcript_path``, ``workspace``, ``model``, etc.) arrives on stdin.

This command:
1. Reads the JSON payload from stdin.
2. Extracts ``session_id``.
3. Checks the workspace private list.
4. Prints ``🔒 agnes-private`` if the session is private; otherwise, when
   the last `agnes update` convergence (#744, see below) changed something
   and hasn't been shown yet, prints a one-line summary; otherwise prints
   nothing (empty status bar segment — lets other tools paint the rest).

Performance: this is invoked on EVERY status-bar refresh, which can be
once per second or more. The implementation is intentionally minimal —
stdlib-only, single file read, no API calls, no heavy imports.

All failure modes (malformed JSON, missing session_id, unreadable list)
end with exit 0 + empty stdout. Polluting Claude Code's status bar with
errors would be worse than a missing indicator.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

import typer

from cli.lib.private_list import is_private
from cli.upgrade_status import read_status


# A Windows deferred self-update (cli/commands/_win_deferred_update) drops this
# sentinel in the config dir while it swaps the tool venv. Every render of this
# statusline is a fresh ``agnes`` process running FROM that venv, so while the
# swap is in flight we step aside (empty output, exit at once) rather than
# relaunch — and re-lock — the very files ``uv tool install --force`` must
# replace. Windows-only: POSIX upgrades in place and never writes the sentinel,
# so the check is skipped entirely there (no extra stat on the hot path).
_IS_WINDOWS = sys.platform == "win32"
_DEFERRED_UPDATE_SENTINEL = "deferred-update.active"
_DEFERRED_UPDATE_TTL_S = 600.0  # ignore a stale sentinel from a crashed helper


def _deferred_update_in_progress() -> bool:
    """True iff a Windows deferred self-update is mid-swap (a fresh sentinel)."""
    if not _IS_WINDOWS:
        return False
    base = os.environ.get("AGNES_CONFIG_DIR") or os.path.expanduser("~/.config/agnes")
    try:
        age = time.time() - os.stat(os.path.join(base, _DEFERRED_UPDATE_SENTINEL)).st_mtime
    except OSError:
        return False
    return age < _DEFERRED_UPDATE_TTL_S


# --------------------------------------------------------------------------- #
# #744 — one-line "what changed" summary after an `agnes update` convergence.
#
# The writer is unchanged: `agnes update` (cli/commands/update.py) already
# appends one JSON line per run to `<workspace>/.claude/agnes/update.log`:
# `{ts, agnes_version, workspace, steps: [{stage, status, detail}, ...]}`.
# This reads the LAST line and, when it is a fresh convergence that actually
# changed something (any step whose status isn't "ok"/"skipped") and hasn't
# been shown yet, renders a compact summary line — once, gated on the report
# `ts` via a small marker file next to the log.
#
# CLI honesty nuance: a freshly-installed CLI binary only activates on the
# NEXT `agnes` invocation (the running interpreter can't replace itself), so
# the "cli" stage compares the version the update.log detail names against
# the version ACTUALLY running right now (a fresh read at render time) to
# decide "already active" vs "active next session". On Windows the real
# swap happens in a detached helper *after* `update.log`'s "staged" line is
# written, so for that status the true success/failure is only known via
# `upgrade_status.json` (which the helper also writes) — used here purely to
# catch a failed deferred swap, not for version numbers.
# --------------------------------------------------------------------------- #

_UPDATE_LOG_RELPATH = (".claude", "agnes", "update.log")
_SUMMARY_MARKER_NAME = ".update-summary-shown"
_MAX_SUMMARY_LEN = 80
_NOISE_STATUSES = {"ok", "skipped"}


def _running_version() -> Optional[str]:
    """Best-effort read of the CLI version actually running right now."""
    try:
        import importlib.metadata as _md

        return _md.version("agnes-the-ai-analyst")
    except Exception:
        return None


def _update_log_path(workspace: Path) -> Path:
    return workspace.joinpath(*_UPDATE_LOG_RELPATH)


def _summary_marker_path(workspace: Path) -> Path:
    return _update_log_path(workspace).with_name(_SUMMARY_MARKER_NAME)


def _read_last_update_entry(workspace: Path) -> Optional[dict]:
    """Return the last JSON line of `update.log` as a dict, or None on any
    missing-file / read / parse problem (never raises)."""
    try:
        text = _update_log_path(workspace).read_text(encoding="utf-8")
    except OSError:
        return None
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    try:
        data = json.loads(lines[-1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _already_shown(workspace: Path, ts: str) -> bool:
    try:
        return _summary_marker_path(workspace).read_text(encoding="utf-8").strip() == ts
    except OSError:
        return False


def _mark_shown(workspace: Path, ts: str) -> None:
    try:
        marker = _summary_marker_path(workspace)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(ts, encoding="utf-8")
    except OSError:
        pass  # best-effort — worst case the line repeats once more


_VERSION_ARROW_RE = re.compile(r"^(\S+)\s*->\s*(\S+)")
_PLUGIN_COUNT_RE = re.compile(r"re-enabled (\d+)")


def _cli_phrase(step: dict) -> Optional[str]:
    """Render the "cli" stage, honest about staged-vs-live activation."""
    status = step.get("status")
    detail = step.get("detail")
    if status == "error":
        return "CLI update failed"
    if status not in ("updated", "staged", "deferred"):
        return None
    if not isinstance(detail, str):
        return None
    match = _VERSION_ARROW_RE.match(detail)
    if not match:
        return None
    from_version, to_version = match.group(1), match.group(2)
    if status == "deferred":
        # The update was intentionally not applied this run (e.g. no safe
        # rollback artifact yet) — "active next session" would overpromise.
        return f"CLI {from_version} -> {to_version} (deferred, retrying next session)"
    if status == "staged":
        # Windows: the real outcome lands later, written by the detached
        # helper to upgrade_status.json — `update.log`'s "staged" line alone
        # can't tell success from failure.
        if read_status().get("last_outcome") == "failure":
            return "CLI update failed"
    running = _running_version()
    if running and running == to_version:
        return f"CLI now {to_version}"
    return f"CLI {from_version} -> {to_version} (active next session)"


def _stage_phrase(step: dict) -> Optional[str]:
    stage = step.get("stage")
    status = step.get("status")
    detail = step.get("detail")
    if stage == "cli":
        return _cli_phrase(step)
    if stage == "workspace":
        if status == "merged":
            return "workspace updated"
        if status == "refreshed":
            return "workspace refreshed"
        if status == "error":
            return "workspace error"
        return None
    if stage == "marketplace":
        if status == "enabled":
            count_match = _PLUGIN_COUNT_RE.search(detail) if isinstance(detail, str) else None
            if count_match:
                n = int(count_match.group(1))
                return f"+{n} plugin{'s' if n != 1 else ''}"
            return "plugins updated"
        if status in ("bootstrapped", "reconciled"):
            return "marketplace updated"
        if status == "error":
            return "marketplace error"
        return None
    if stage == "pull" and status == "error":
        return "data pull error"
    if stage == "env" and status == "error":
        return "env error"
    if stage == "config" and status == "error":
        return "config error"
    return None


def _truncate(line: str, max_len: int) -> str:
    if len(line) <= max_len:
        return line
    return line[: max_len - 1].rstrip() + "…"


def _update_summary_line(workspace: Path) -> Optional[str]:
    entry = _read_last_update_entry(workspace)
    if entry is None:
        return None
    ts = entry.get("ts")
    if not isinstance(ts, str) or not ts:
        return None
    if _already_shown(workspace, ts):
        return None

    steps = entry.get("steps")
    if not isinstance(steps, list):
        return None
    changed = [s for s in steps if isinstance(s, dict) and s.get("status") not in _NOISE_STATUSES]
    if not changed:
        return None  # nothing actually changed — render nothing (do not mark shown)

    parts = [phrase for phrase in (_stage_phrase(step) for step in changed) if phrase]
    if not parts:
        return None

    _mark_shown(workspace, ts)  # show at most once for this report
    return _truncate("Agnes: " + " · ".join(parts), _MAX_SUMMARY_LEN)


def _update_summary_line_safe(workspace: Path) -> Optional[str]:
    """Never-raise wrapper — statusline must stay best-effort."""
    try:
        return _update_summary_line(workspace)
    except Exception:
        return None


statusline_app = typer.Typer(
    help="Status-line helper for Claude Code — prints '🔒 agnes-private' when the current session is private, "
    "or a one-line summary of what the last `agnes update` convergence changed.",
)


@statusline_app.callback(invoke_without_command=True)
def statusline() -> None:
    """Read stdin session JSON; emit private indicator or update-summary line."""
    # Windows deferred self-update in flight: step aside at once so this render
    # isn't relaunching (and re-locking) the tool venv the swap must replace.
    # Claude Code keeps the previous status bar line. Inert off-Windows.
    if _deferred_update_in_progress():
        return
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError):
        return  # silent — never poison the status bar

    if not isinstance(payload, dict):
        return

    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return

    workspace = Path(os.environ.get("AGNES_LOCAL_DIR", ".")).resolve()
    try:
        if is_private(workspace, session_id):
            typer.echo("🔒 agnes-private")
            return
    except OSError:
        return  # filesystem hiccup — empty output is better than a crash

    # Private marker takes precedence over the update summary (both are
    # single-line status segments; showing both would blow the width budget
    # and the private indicator is the higher-priority signal).
    line = _update_summary_line_safe(workspace)
    if line:
        typer.echo(line)
