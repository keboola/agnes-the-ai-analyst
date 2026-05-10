"""`agnes capture-session` — SessionStart hook helper.

Reads the Claude Code hook payload from stdin (a JSON object containing
``transcript_path``), extracts the absolute path to the current session's
``.jsonl`` transcript, and appends it to ``<workspace>/.claude/agnes-sessions.txt``.

The queue file feeds ``agnes push``: rather than reverse-engineer Claude
Code's cwd-to-folder encoding (an internal implementation detail), we use
the ``transcript_path`` field of the hook stdin JSON, which is part of the
documented hook contract.

Failure modes — silent exit code 0 in all cases, since this command runs
inside a SessionStart hook chain and a noisy failure would clutter Claude
Code's startup output:
- stdin not JSON → no-op
- JSON missing ``transcript_path`` → no-op
- ``transcript_path`` empty → no-op
- Workspace ``.claude/`` not writable → no-op (best-effort, hook continues)

Diagnostic stderr output only when ``--verbose`` is set, for debugging
hook misconfiguration. The hook command in ``cli/lib/hooks.py`` does NOT
pass ``--verbose`` in production.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import typer

from cli.lib.session_queue import append_to_queue


capture_session_app = typer.Typer(
    help="Capture the current Claude Code session's transcript path into the upload queue.",
)


@capture_session_app.callback(invoke_without_command=True)
def capture_session(
    verbose: bool = typer.Option(
        False, "--verbose", help="Log diagnostic info to stderr (off by default)."
    ),
) -> None:
    """Read SessionStart hook stdin JSON and append transcript_path to queue."""
    workspace = Path(os.environ.get("AGNES_LOCAL_DIR", ".")).resolve()

    try:
        raw = sys.stdin.read()
    except Exception as exc:
        if verbose:
            typer.echo(f"capture-session: stdin read failed: {exc}", err=True)
        return

    if not raw.strip():
        if verbose:
            typer.echo("capture-session: empty stdin, nothing to capture.", err=True)
        return

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        if verbose:
            typer.echo(f"capture-session: stdin not valid JSON: {exc}", err=True)
        return

    transcript_path = payload.get("transcript_path") if isinstance(payload, dict) else None
    if not transcript_path or not isinstance(transcript_path, str):
        if verbose:
            typer.echo("capture-session: payload missing transcript_path.", err=True)
        return

    try:
        append_to_queue(workspace, transcript_path)
    except OSError as exc:
        if verbose:
            typer.echo(
                f"capture-session: append to queue failed ({workspace}): {exc}",
                err=True,
            )
        return

    if verbose:
        typer.echo(f"capture-session: queued {transcript_path}", err=True)
