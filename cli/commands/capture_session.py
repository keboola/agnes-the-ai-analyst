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

Operability breadcrumb: every invocation (success OR silent-failure path)
appends one line to ``<workspace>/.claude/agnes-capture-session.log``
with the timestamp and outcome. ``agnes diagnose`` (and ad-hoc ops
inspection) can read the tail of this log to flag "hook is firing but
queue stays empty" — without it, an upstream Claude Code contract change
(e.g. stdin payload schema shifts) is invisible to operators because the
hook always exits 0 (David's #11 from the PR review).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import typer

from cli.lib.private_list import is_private
from cli.lib.session_queue import append_to_queue


_BREADCRUMB_FILENAME = "agnes-capture-session.log"
_BREADCRUMB_MAX_BYTES = 256 * 1024  # 256 KiB rolling cap; ~3000 lines


def _record_breadcrumb(workspace: Path, outcome: str, detail: str = "") -> None:
    """Append one TSV line to the capture-session breadcrumb log.

    Format: ``<iso_ts>\\t<outcome>\\t<detail>``. Outcomes:
    ``ok`` (queued), ``private_skip`` (matched private list),
    ``empty_stdin``, ``bad_json``, ``not_object``, ``no_transcript_path``,
    ``stdin_read_error``, ``write_error``.

    Best-effort: a failure here MUST NOT escape — this is the hook's
    silent-failure observability layer; raising would defeat the point.
    Rolls the log file when it crosses 256 KiB so it doesn't grow
    unboundedly on a long-lived workspace.
    """
    try:
        claude_dir = workspace / ".claude"
        if not claude_dir.exists():
            # Don't materialize .claude/ in non-Agnes directories — same
            # rationale as private_list._claude_dir_readonly. If the dir
            # doesn't exist, capture-session is a no-op anyway.
            return
        path = claude_dir / _BREADCRUMB_FILENAME
        try:
            if path.stat().st_size > _BREADCRUMB_MAX_BYTES:
                path.unlink()
        except OSError:
            pass
        line = "\t".join([
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            outcome,
            detail.replace("\t", " ").replace("\n", " ")[:200],
        ]) + "\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        # Truly best-effort: any breadcrumb failure is swallowed so the
        # capture-session contract stays "exit 0 always".
        pass


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
        _record_breadcrumb(workspace, "stdin_read_error", str(exc))
        return

    if not raw.strip():
        if verbose:
            typer.echo("capture-session: empty stdin, nothing to capture.", err=True)
        _record_breadcrumb(workspace, "empty_stdin")
        return

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        if verbose:
            typer.echo(f"capture-session: stdin not valid JSON: {exc}", err=True)
        _record_breadcrumb(workspace, "bad_json", str(exc))
        return

    if not isinstance(payload, dict):
        if verbose:
            typer.echo("capture-session: payload is not a JSON object.", err=True)
        _record_breadcrumb(workspace, "not_object", type(payload).__name__)
        return

    transcript_path = payload.get("transcript_path")
    if not transcript_path or not isinstance(transcript_path, str):
        if verbose:
            typer.echo("capture-session: payload missing transcript_path.", err=True)
        _record_breadcrumb(workspace, "no_transcript_path")
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
        _record_breadcrumb(workspace, "private_skip", session_id)
        return

    try:
        append_to_queue(workspace, session_id, transcript_path)
    except OSError as exc:
        if verbose:
            typer.echo(
                f"capture-session: append to queue failed ({workspace}): {exc}",
                err=True,
            )
        _record_breadcrumb(workspace, "write_error", str(exc))
        return

    _record_breadcrumb(workspace, "ok", session_id or "(no-sid)")
    if verbose:
        typer.echo(f"capture-session: queued {transcript_path}", err=True)
