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
    skip_materialize: bool = typer.Option(
        False,
        "--skip-materialize",
        help=(
            "Skip materialized-mode tables (server-side scheduled BQ "
            "scan results, often multi-GB). Their data is still discoverable "
            "via `agnes catalog` and remote-mode tables still pull. Useful "
            "for a fast first init when an analyst only needs --remote access."
        ),
    ),
):
    """Refresh data from the server into ./server/parquet + ./user/duckdb."""
    server_url = get_server_url()
    if not server_url:
        # `get_server_url()` falls back to a localhost default today, so this
        # branch is mostly a defensive guard — if a future config change ever
        # returns an empty string we still want a friendly hint, not a crash
        # halfway through the manifest fetch.
        typer.echo(
            render_error(
                0,
                {
                    "detail": {
                        "kind": "server_unreachable",
                        "hint": "No server configured. Run: agnes init --server-url <URL> --token <PAT>",
                    }
                },
            ),
            err=True,
        )
        raise typer.Exit(1)

    token = get_token()
    if not token:
        typer.echo(
            render_error(
                0,
                {
                    "detail": {
                        "kind": "auth_failed",
                        "hint": "No token. Run: agnes auth import-token --token <PAT>",
                    }
                },
            ),
            err=True,
        )
        raise typer.Exit(1)

    workspace = Path(os.environ.get("AGNES_LOCAL_DIR", ".")).resolve()

    # Legacy-hook nudge (#478): workspaces bootstrapped by the OLD server
    # flow (a `collect_session` / `server/scripts/` SessionEnd hook, no
    # `agnes init` hooks) never invoke `agnes self-upgrade`, so their CLI
    # drifts stale forever. Emit ONE stderr line pointing the analyst at
    # `agnes init`. We do NOT auto-migrate — the analyst owns when their
    # hook layout changes. Suppressed under --quiet (the SessionStart hook
    # path, kept silent) and --json (machine-readable output). Best-effort:
    # a malformed settings.json must never abort the pull.
    if not (quiet or as_json):
        try:
            from cli.lib.hooks import workspace_has_legacy_hooks

            if workspace_has_legacy_hooks(workspace):
                typer.echo(
                    "This workspace uses an outdated hook layout — run `agnes init` to enable auto-update.",
                    err=True,
                )
        except Exception:
            pass

    # Lazy TTL sweep (#407): drop any `--ttl` snapshots whose expiry has
    # elapsed before refreshing. Best-effort and fully wrapped — a sweep
    # failure (locking quirk, permissions) must NEVER block a pull, which is
    # the load-bearing SessionStart hook. Skip under --dry-run (no disk
    # writes anywhere) and --json (machine-readable output stays clean).
    if not dry_run:
        try:
            from cli.snapshot_meta import sweep_expired_snapshots

            swept = sweep_expired_snapshots(workspace / "user" / "snapshots")
            if swept and not (quiet or as_json):
                for name in swept:
                    typer.echo(f"swept expired snapshot: {name}", err=True)
        except Exception:
            # Intentionally swallowed — see the comment above.
            pass

    # Show progress unless quiet (SessionStart hooks) or json (machine-
    # readable output where Rich's terminal-control sequences would be
    # garbage in the consumer's parser).
    show_progress = not (quiet or as_json)
    try:
        result: PullResult = run_pull(
            server_url,
            token,
            workspace,
            dry_run=dry_run,
            skip_materialize=skip_materialize,
            show_progress=show_progress,
        )
    except Exception as exc:
        # `run_pull` is documented to record per-table / per-stage failures
        # under `result.errors` rather than raising, so reaching this branch
        # means something genuinely unexpected blew up (e.g. a programming
        # error in a helper). Render it through the same typed-error pipe so
        # the operator gets a consistent shape, then exit non-zero.
        typer.echo(
            render_error(
                0,
                {
                    "detail": {
                        "kind": "manifest_unauthorized",
                        "hint": f"Pull failed: {exc}",
                        "message": str(exc),
                    }
                },
            ),
            err=True,
        )
        raise typer.Exit(1)

    if as_json:
        typer.echo(
            json.dumps(
                {
                    "tables_updated": result.tables_updated,
                    "tables_removed": result.tables_removed,
                    "parquets_total": result.parquets_total,
                    "rules_count": result.rules_count,
                    # WF-4 (wave 2H) — provenance counters. `getattr` with a
                    # 0 default keeps this endpoint tolerant of duck-typed
                    # `PullResult` stand-ins in tests that predate these
                    # fields (`_FakePullResult` et al. in test_cli_pull.py).
                    "tables_via_signed_url": getattr(result, "tables_via_signed_url", 0),
                    "tables_via_app": getattr(result, "tables_via_app", 0),
                    "duration_s": round(result.duration_s, 3),
                    "errors": result.errors,
                }
            )
        )
        # #596 — a per-table / per-stage failure must surface as a non-zero
        # exit even on the machine-readable path. Emit the JSON dict first
        # (the consumer parses `errors`), THEN exit 1 so a wrapping script's
        # `set -e` / `||` reacts to the failure.
        if result.errors:
            raise typer.Exit(1)
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
            # #596 — even in the silent SessionStart-hook path, a table that
            # failed to land must exit non-zero so the canonical hook's
            # trailing `|| true` is what swallows it (a deliberate operator
            # choice), not a hidden exit 0 that hides data loss.
            raise typer.Exit(1)
        return

    # Surface tables_removed alongside tables_updated so an operator who
    # dropped a data package from their stack sees the prune count in the
    # primary summary line — not just buried in the per-type status block
    # below. Pruning is a security-relevant op (revokes local query access);
    # silent removals were the Devin Review finding on #594.
    if result.tables_removed:
        typer.echo(
            f"Updated {result.tables_updated} tables, removed {result.tables_removed} ({result.parquets_total} total)."
        )
    else:
        typer.echo(f"Updated {result.tables_updated} tables ({result.parquets_total} total).")
    typer.echo(f"Rules: {result.rules_count}.")

    # WF-4 (wave 2H) — provenance summary. Only printed once a
    # `signed_url` has actually been used (an instance with no object
    # store configured, or `distribution.signed_urls: off`, never sees
    # this line — no noise for the common case). `getattr` keeps this
    # tolerant of duck-typed `PullResult` stand-ins in tests.
    via_signed_url = getattr(result, "tables_via_signed_url", 0)
    if via_signed_url:
        via_app = getattr(result, "tables_via_app", 0)
        typer.echo(f"  {via_signed_url} via signed URL, {via_app} via app path.")

    # #754 — an empty manifest with zero errors is ambiguous: it means
    # either "nothing is registered on the server yet" or "your account
    # has no group grants for any table" — NOT a transport/server failure
    # (those already land in `result.errors` via the `except` in
    # `run_pull`'s manifest-fetch step and are reported below). Pre-fix
    # this printed only the bare "Updated 0 tables (0 total)." line above
    # with no hint which of the two it was.
    if result.parquets_total == 0 and not result.errors:
        typer.echo(
            "No tables available to pull — either nothing is registered on "
            "the server yet, or your account has no group grants for any "
            "table. Ask your admin to register a table and/or grant your "
            "group access (`agnes admin grant`)."
        )

    # v49 (Task 8.12): per-type status block surfaced from `SyncReport`.
    # The new per-type sync loop in ``cli/lib/pull_sync.py`` reports
    # added/updated/removed counts for direct_tables, data_packages, and
    # memory_domains; rendering them here lets the operator see at a
    # glance what changed without trawling debug logs. Skipped when the
    # manifest predates v49 (no `stack_sync` on PullResult) so older
    # servers still produce the legacy two-line output.
    stack = getattr(result, "stack_sync", None)
    if stack is not None:
        _emit_stack_sync_block(stack)

    if result.errors:
        for e in result.errors:
            typer.echo(f"warn: {e}", err=True)
        # #596 — a partial pull (any table failed to land) must exit non-zero
        # so manual invocation and CI both see the failure instead of a
        # success-looking exit 0 that silently hides missing tables.
        raise typer.Exit(1)


def _emit_stack_sync_block(stack) -> None:
    """Print the v49 per-type ``SyncReport`` summary.

    Format mirrors the rest of `agnes pull`'s output: plain text, one
    line per type. Lines are emitted only when something changed for
    that type — a clean idempotent pull stays as quiet as before
    (just the legacy "Updated 0 tables …" header).

    Layout::

        Stack sync:
          marketplace_plugins: ✓ 0 changes
          data_packages:       2 added, 1 updated, 0 removed
          memory_domains:      ✓ 0 changes
          direct_tables:       ✓ 0 changes

    Invariant violations (if any) surface as a trailing warning so a
    drifted disk state isn't silently swept under the rug.
    """

    # Tolerate either dataclass shape (real ``SyncReport``) or test
    # doubles supplying a duck-typed object with .direct_tables etc.
    def _line(label: str, rep) -> str:
        added = getattr(rep, "added", 0)
        updated = getattr(rep, "updated", 0)
        removed = getattr(rep, "removed", 0)
        if not (added or updated or removed):
            return f"  {label:<22} ✓ 0 changes"
        parts = []
        if added:
            parts.append(f"{added} added")
        if updated:
            parts.append(f"{updated} updated")
        if removed:
            parts.append(f"{removed} removed")
        return f"  {label:<22} {', '.join(parts)}"

    direct = getattr(stack, "direct_tables", None)
    pkgs = getattr(stack, "data_packages", None)
    mem = getattr(stack, "memory_domains", None)
    if direct is None and pkgs is None and mem is None:
        return

    typer.echo("Stack sync:")
    if direct is not None:
        typer.echo(_line("direct_tables:", direct))
    if pkgs is not None:
        typer.echo(_line("data_packages:", pkgs))
    if mem is not None:
        typer.echo(_line("memory_domains:", mem))

    violations = getattr(stack, "invariant_violations", []) or []
    if violations:
        typer.echo(
            f"warn: {len(violations)} stack invariant violation{'s' if len(violations) != 1 else ''} — see logs.",
            err=True,
        )
