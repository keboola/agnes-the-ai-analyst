"""`agnes mark-private` — mark the current Claude Code session as private.

Invoked by the `/agnes-private` slash command (deterministic ``!``-prefix
direct bash, no AI in the loop). Reads ``CLAUDE_CODE_SESSION_ID`` from the
environment — Claude Code sets this variable in every Bash/PowerShell
subprocess it spawns (documented stable API).

Adds the session_id to ``<workspace_root>/.claude/agnes-sessions-private.txt``.
That file is the authoritative source for "do not upload" — ``agnes push``
consults it and skips any matching session_id. The workspace is anchored to
the ``workspace_root`` config key (the same anchor ``push`` uses) so the
private list and the upload scan always agree on which workspace they mean.
Falls back to the current directory only when ``workspace_root`` is unset
(a fresh client before the first ``agnes init`` / self-upgrade back-fill),
where cwd equals the workspace root anyway.

Refuses to run outside a Claude Code session (no ``CLAUDE_CODE_SESSION_ID``)
to make accidental CLI invocations from a regular terminal obvious.
"""

from __future__ import annotations

import os
from pathlib import Path

import typer

from cli.config import get_workspace_root
from cli.lib.private_list import add_private


mark_private_app = typer.Typer(
    help="Mark the current Claude Code session as private — exclude it from `agnes push`.",
)


@mark_private_app.callback(invoke_without_command=True)
def mark_private() -> None:
    """Add CLAUDE_CODE_SESSION_ID to the workspace private list."""
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
    if not session_id:
        typer.echo(
            "Error: CLAUDE_CODE_SESSION_ID is not set. "
            "Run this inside a Claude Code session (via /agnes-private).",
            err=True,
        )
        raise typer.Exit(1)

    workspace_root = get_workspace_root()
    workspace = (
        Path(workspace_root)
        if workspace_root
        else Path(os.environ.get("AGNES_LOCAL_DIR", ".")).resolve()
    )
    newly_added = add_private(workspace, session_id)

    if newly_added:
        typer.echo(
            f"Session {session_id} marked as private. "
            "Its transcript will not be uploaded by `agnes push`."
        )
    else:
        typer.echo(f"Session {session_id} is already marked as private. No change.")
