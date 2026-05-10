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

from cli.lib.private_list import is_private
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
    """Read SessionStart hook stdin JSON and append (session_id, transcript_path) to queue.

    Honors the private list: if the session_id is already marked private
    (e.g. user ran `/agnes-private` before this hook chain reached
    capture-session), the queue write is skipped so the session never
    enters the upload pipeline.
    """
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

    if not isinstance(payload, dict):
        if verbose:
            typer.echo("capture-session: payload is not a JSON object.", err=True)
        return

    transcript_path = payload.get("transcript_path")
    if not transcript_path or not isinstance(transcript_path, str):
        if verbose:
            typer.echo("capture-session: payload missing transcript_path.", err=True)
        return

    session_id = payload.get("session_id") or ""
    if not isinstance(session_id, str):
        session_id = ""

    # Race protection: user may have run /agnes-private BEFORE this hook
    # got a chance to write. Skip the queue append in that case — the
    # private list is the authoritative source for "do not upload".
    if session_id and is_private(workspace, session_id):
        if verbose:
            typer.echo(
                f"capture-session: session {session_id} is private; skipping queue.",
                err=True,
            )
        return

    try:
        append_to_queue(workspace, session_id, transcript_path)
    except OSError as exc:
        if verbose:
            typer.echo(
                f"capture-session: append to queue failed ({workspace}): {exc}",
                err=True,
            )
        return

    if verbose:
        typer.echo(f"capture-session: queued {transcript_path}", err=True)
