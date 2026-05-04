"""`agnes push` - upload session jsonl + CLAUDE.local.md to the server.

Thin Typer wrapper extracted from the legacy `agnes sync --upload-only`
path in `cli/commands/sync.py`. Used by:
- Manual invocation: analyst types `agnes push` to force an upload.
- SessionEnd hook: `agnes push --quiet 2>/dev/null || true` runs at the
  end of every Claude Code session in this workspace.

Lazy on-disk contract: when there are no `user/sessions/*.jsonl` files
and no `.claude/CLAUDE.local.md`, this command must NOT create
`user/sessions/` (or any other directory). Tests pin the lazy mkdir
contract so the empty-workspace case stays a true no-op on disk.

Errors render via `cli/error_render.py:render_error()` for typed-error
shape consistency with `agnes pull`.

Task 18 will register `push_app` on the root Typer app and delete the
legacy `agnes sync --upload-only` flag. Until then this module is
callable only via direct import (which is exactly what the test does).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import typer

from cli.client import api_post
from cli.config import get_server_url, get_token
from cli.error_render import render_error


push_app = typer.Typer(help="Upload sessions and CLAUDE.local.md to the server")


@push_app.callback(invoke_without_command=True)
def push(
    quiet: bool = typer.Option(False, "--quiet", help="Suppress success stdout (errors still surface on stderr)"),
    as_json: bool = typer.Option(False, "--json", help="Emit a single JSON object summarizing the upload"),
    dry_run: bool = typer.Option(False, "--dry-run", help="List what would be uploaded without sending anything"),
):
    """Upload session jsonl + CLAUDE.local.md from ./user/sessions and ./.claude."""
    server_url = get_server_url()
    if not server_url:
        # `get_server_url()` falls back to a localhost default today, so this
        # branch is mostly a defensive guard for a future config change that
        # might return an empty string.
        typer.echo(
            render_error(0, {"detail": {
                "kind": "server_unreachable",
                "hint": "No server configured. Run: agnes init --server-url <URL> --token <PAT>",
            }}),
            err=True,
        )
        raise typer.Exit(1)

    token = get_token()
    if not token:
        typer.echo(
            render_error(0, {"detail": {
                "kind": "auth_failed",
                "hint": "No token. Run: agnes auth import-token --token <PAT>",
            }}),
            err=True,
        )
        raise typer.Exit(1)

    workspace = Path(os.environ.get("AGNES_LOCAL_DIR", ".")).resolve()
    sessions_dir = workspace / "user" / "sessions"
    local_md = workspace / ".claude" / "CLAUDE.local.md"

    # Lazy: only enumerate when the directory actually exists. We must not
    # mkdir here - the empty-workspace case must leave disk untouched so
    # the SessionEnd hook stays a true no-op for analysts who haven't
    # produced any sessions yet.
    session_files = (
        sorted(sessions_dir.glob("*.jsonl")) if sessions_dir.exists() else []
    )
    has_local_md = local_md.exists()

    if dry_run:
        plan = {
            "dry_run": True,
            "would_upload": {
                "sessions": [str(f) for f in session_files],
                "local_md": str(local_md) if has_local_md else None,
            },
            "summary": {
                "sessions_count": len(session_files),
                "local_md_present": has_local_md,
            },
        }
        if as_json:
            typer.echo(json.dumps(plan, indent=2))
            return
        if quiet:
            return
        typer.echo(f"Dry run - would upload {len(session_files)} session file(s)")
        for f in session_files:
            typer.echo(f"  {f}")
        if has_local_md:
            typer.echo(f"Would upload CLAUDE.local.md  ({local_md})")
        else:
            typer.echo("No CLAUDE.local.md to upload")
        return

    results = {"sessions": 0, "local_md": False, "errors": []}

    # Upload sessions. Per-file failures are recorded into `errors` and the
    # loop continues - one corrupt jsonl mustn't block the rest, and a
    # transient 5xx on one file shouldn't poison the whole upload.
    for f in session_files:
        try:
            with open(f, "rb") as fh:
                resp = api_post("/api/upload/sessions", files={"file": (f.name, fh)})
            if resp.status_code == 200:
                results["sessions"] += 1
            else:
                results["errors"].append(
                    {"file": f.name, "status": resp.status_code}
                )
        except Exception as exc:
            results["errors"].append({"file": f.name, "error": str(exc)})

    # Upload CLAUDE.local.md
    if has_local_md:
        try:
            content = local_md.read_text(encoding="utf-8")
            resp = api_post("/api/upload/local-md", json={"content": content})
            if resp.status_code == 200:
                results["local_md"] = True
            else:
                results["errors"].append(
                    {"file": "CLAUDE.local.md", "status": resp.status_code}
                )
        except Exception as exc:
            results["errors"].append({"file": "CLAUDE.local.md", "error": str(exc)})

    if as_json:
        typer.echo(json.dumps(results))
        return

    if quiet:
        # Quiet mode is for the SessionEnd hook - silent on success so
        # Claude Code's stdout stays clean. Errors still flow to stderr.
        if results["errors"]:
            for e in results["errors"]:
                typer.echo(f"warn: {e}", err=True)
        return

    typer.echo(f"Uploaded {results['sessions']} sessions")
    if results["local_md"]:
        typer.echo("Uploaded CLAUDE.local.md")
    if results["errors"]:
        for e in results["errors"]:
            typer.echo(f"warn: {e}", err=True)
