"""`agnes pull` — refresh registered data into the workspace.

Thin Typer wrapper around `cli/lib/pull.py:run_pull`. Used by:
- Manual invocation: analyst types `agnes pull` to force a refresh.
- SessionStart hook: `agnes pull --quiet 2>/dev/null || true` runs at the start
  of every Claude Code session in this workspace.

Errors render via `cli/error_render.py:render_error()` for typed-error
shape consistency with other CLI commands. The wrapper intentionally does
no I/O of its own — config lookup, manifest fetch, parquet download, view
rebuild, and rules-bundle write all live in `run_pull`. This keeps the
command code trivially testable and the data-refresh primitive reusable
from other entrypoints (init, analyst setup, future MCP tools).

Task 18 will register `pull_app` on the root Typer app and delete the
legacy `agnes sync` command. Until then this module is callable only via
direct import (which is exactly what the test does).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import typer

from cli.config import get_server_url, get_token
from cli.error_render import render_error
from cli.lib.pull import PullResult, run_pull


pull_app = typer.Typer(help="Refresh registered data from the server")


@pull_app.callback(invoke_without_command=True)
def pull(
    quiet: bool = typer.Option(False, "--quiet", help="Suppress success stdout (errors still surface on stderr)"),
    as_json: bool = typer.Option(False, "--json", help="Emit a single JSON object summarizing the pull"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Compute the delta without writing anything to disk"),
):
    """Refresh data from the server into ./server/parquet + ./user/duckdb."""
    server_url = get_server_url()
    if not server_url:
        # `get_server_url()` falls back to a localhost default today, so this
        # branch is mostly a defensive guard — if a future config change ever
        # returns an empty string we still want a friendly hint, not a crash
        # halfway through the manifest fetch.
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

    try:
        result: PullResult = run_pull(server_url, token, workspace, dry_run=dry_run)
    except Exception as exc:
        # `run_pull` is documented to record per-table / per-stage failures
        # under `result.errors` rather than raising, so reaching this branch
        # means something genuinely unexpected blew up (e.g. a programming
        # error in a helper). Render it through the same typed-error pipe so
        # the operator gets a consistent shape, then exit non-zero.
        typer.echo(
            render_error(0, {"detail": {
                "kind": "manifest_unauthorized",
                "hint": f"Pull failed: {exc}",
                "message": str(exc),
            }}),
            err=True,
        )
        raise typer.Exit(1)

    if as_json:
        typer.echo(json.dumps({
            "tables_updated": result.tables_updated,
            "parquets_total": result.parquets_total,
            "rules_count": result.rules_count,
            "duration_s": round(result.duration_s, 3),
            "errors": result.errors,
        }))
        return

    if quiet:
        # Quiet mode is for the SessionStart hook — silent on success so
        # Claude Code's stdout stays clean. Errors still flow to stderr so
        # the user sees them in their terminal even when the hook redirects
        # `2>/dev/null` (the hook explicitly forwards stderr too in the
        # canonical `agnes init` template).
        if result.errors:
            for e in result.errors:
                typer.echo(f"warn: {e}", err=True)
        return

    typer.echo(f"Updated {result.tables_updated} tables ({result.parquets_total} total).")
    typer.echo(f"Rules: {result.rules_count}.")
    if result.errors:
        for e in result.errors:
            typer.echo(f"warn: {e}", err=True)
