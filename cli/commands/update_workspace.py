"""`agnes update-workspace` — safe re-apply of the Initial Workspace
Template (IWT) into an already-initialised workspace.

Unlike `agnes init --force` (a bootstrap command that also re-pulls all
parquets and requires an explicit `--server-url`), this command:

- reads the server URL + PAT from saved config (`~/.config/agnes/`), like
  `agnes pull` — no `--server-url`/`--token` needed,
- does NOT re-pull parquets (that's `agnes pull` / the SessionStart hook),
- BACKS UP files the analyst changed to `<name>.bak.<timestamp>` before
  overwriting (3-way diff against the stored baseline at
  `.claude/agnes/installed-template.zip`),
- leaves files not in the template untouched.

IWT-ONLY. On an instance with no Initial Workspace Template configured the
command is a clean no-op — it touches nothing and exits 0. The actual
warning + confirmation live here (CLI is the single source of truth); the
`/update-workspace` slash command is a thin wrapper that runs `--dry-run`
to preview, then `--yes` to apply after the analyst confirms.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from cli.config import get_server_url, get_token
from cli.error_render import render_error
from cli.lib.initial_workspace import (
    apply_update,
    download_zip,
    preview_update,
    probe_status,
    prompt_update_confirmation,
)


update_workspace_app = typer.Typer(
    help="Safely re-apply the Initial Workspace Template into this workspace"
)


def _agnes_version() -> str:
    try:
        import importlib.metadata as _md

        return _md.version("agnes-the-ai-analyst")
    except Exception:
        return "unknown"


@update_workspace_app.callback(invoke_without_command=True)
def update_workspace(
    workspace_str: Optional[str] = typer.Option(
        None, "--workspace", help="Target workspace dir (default: cwd)"
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y",
        help="Skip the interactive confirmation (used by the /update-workspace slash command after the analyst confirms).",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Show what would change (created / updated / backed-up) without writing anything.",
    ),
):
    """Re-apply the latest IWT, backing up files you changed to `.bak`."""
    workspace = Path(workspace_str).resolve() if workspace_str else Path.cwd()

    # ------------------------------------------------------------------
    # Resolve server URL + PAT from saved config (like `agnes pull`).
    # ------------------------------------------------------------------
    server_url = get_server_url()
    if not server_url:
        typer.echo(render_error(0, {"detail": {
            "kind": "server_unreachable",
            "hint": "No server configured. Run: agnes init --server-url <URL> --token <PAT>",
        }}), err=True)
        raise typer.Exit(1)
    token = get_token()
    if not token:
        typer.echo(render_error(0, {"detail": {
            "kind": "auth_failed",
            "hint": "No token. Run `agnes auth login` or `agnes init` first.",
        }}), err=True)
        raise typer.Exit(1)

    # ------------------------------------------------------------------
    # IWT guard. probe_status returns None on 404 (old server) and a
    # StatusInfo otherwise; it raises typer.Exit on 401 / unexpected
    # status. A non-IWT instance (None, or configured=False) is a clean
    # no-op — we touch NOTHING and exit 0.
    # ------------------------------------------------------------------
    status = probe_status(server_url, token)
    if status is None or not status.configured:
        typer.echo(
            "This Agnes instance has no Initial Workspace Template configured — "
            "nothing to update."
        )
        raise typer.Exit(0)
    if not status.synced:
        typer.echo(render_error(0, {"detail": {
            "kind": "initial_workspace_not_synced",
            "hint": "Template registered but not synced yet — ask an admin to click "
                    "'Sync now' in /admin/server-config.",
        }}), err=True)
        raise typer.Exit(1)

    # ------------------------------------------------------------------
    # Download once, classify, then (preview | confirm + apply).
    # ------------------------------------------------------------------
    new_zip = download_zip(server_url, token)
    plan = preview_update(workspace, new_zip)

    nothing_to_do = not (plan.created or plan.updated or plan.backed_up)
    if nothing_to_do:
        typer.echo("Workspace already matches the latest template — nothing to do.")
        raise typer.Exit(0)

    if dry_run:
        typer.echo("Dry run — no changes written.\n")
        _print_plan(plan)
        raise typer.Exit(0)

    if not yes:
        if not prompt_update_confirmation(workspace, plan):
            typer.echo("Aborted by user; workspace unchanged.", err=True)
            raise typer.Exit(1)

    result = apply_update(
        workspace, new_zip, status, server_url, token,
        agnes_version=_agnes_version(),
    )

    # ------------------------------------------------------------------
    # Report.
    # ------------------------------------------------------------------
    typer.echo("Workspace updated from the Initial Workspace Template.")
    typer.echo(f"  Template : {status.template_source or '—'} "
               f"@ {(status.template_sha or '')[:10] or '—'}")
    typer.echo(f"  Created  : {len(result.created)}")
    typer.echo(f"  Updated  : {len(result.updated)}")
    typer.echo(f"  Backed up: {len(result.backed_up)}")
    for orig, bak in result.backed_up:
        typer.echo(f"    ~ {orig}  →  {bak}")


def _print_plan(plan) -> None:
    """Render an UpdatePlan for `--dry-run` (mirrors the confirmation lists)."""
    if plan.backed_up:
        typer.echo(f"Would back up + update (you changed these) — {len(plan.backed_up)}:")
        for rel in plan.backed_up:
            typer.echo(f"  ~ {rel}")
    if plan.updated:
        typer.echo(f"Would update in place (unchanged by you) — {len(plan.updated)}:")
        for rel in plan.updated:
            typer.echo(f"  · {rel}")
    if plan.created:
        typer.echo(f"Would create — {len(plan.created)}:")
        for rel in plan.created:
            typer.echo(f"  + {rel}")
