"""`agnes statusline` — Claude Code statusLine helper.

Claude Code's ``statusLine`` setting in ``settings.json`` invokes a shell
command on every status-bar render and displays the first line of its
stdout. The session payload (a JSON object containing ``session_id``,
``transcript_path``, ``workspace``, ``model``, etc.) arrives on stdin.

This command:
1. Reads the JSON payload from stdin.
2. Extracts ``session_id``.
3. Checks the workspace private list.
4. Prints ``🔒 agnes-private`` if the session is private; otherwise prints
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
import sys
import time
from pathlib import Path

import typer

from cli.lib.private_list import is_private


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
        age = time.time() - os.stat(
            os.path.join(base, _DEFERRED_UPDATE_SENTINEL)
        ).st_mtime
    except OSError:
        return False
    return age < _DEFERRED_UPDATE_TTL_S


statusline_app = typer.Typer(
    help="Status-line helper for Claude Code — prints '🔒 agnes-private' when the current session is private.",
)


@statusline_app.callback(invoke_without_command=True)
def statusline() -> None:
    """Read stdin session JSON; emit private indicator if the session is private."""
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
    except OSError:
        return  # filesystem hiccup — empty output is better than a crash
