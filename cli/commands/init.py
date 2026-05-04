"""`agnes init` — bootstrap an analyst workspace.

Single-paste flow: web user clicks "Generate prompt" on /setup?role=analyst,
pastes into Claude Code in an empty folder; Claude runs `agnes init` (among
other steps). Non-interactive: --token + --server-url required.

Steps in order:
1. Detect existing workspace (`CLAUDE.md` containing the init marker) — exit 1
   unless --force, with a typed `partial_state` error.
2. Verify the PAT via `GET /api/catalog/tables` — typed `auth_failed` on 401,
   `server_unreachable` on network error.
3. Persist server URL + PAT to `~/.config/agnes/` so subsequent `agnes pull` /
   `agnes push` invocations (including the SessionStart/End hooks installed
   below) inherit the credentials without env vars.
4. Fetch the rendered CLAUDE.md from `GET /api/welcome` (server-rendered,
   RBAC-filtered, role-aware).
5. Seed `.claude/settings.json` with default model + permissions, then call
   `cli.lib.hooks.install_claude_hooks` to merge in the SessionStart/End hook
   commands. Idempotent on re-run.
6. Write the `.claude/CLAUDE.local.md` stub only when absent — `--force`
   regenerates CLAUDE.md but **never** clobbers the operator-edited
   CLAUDE.local.md.
7. Run the first `cli.lib.pull.run_pull` so the workspace ships with current
   parquets, DuckDB views, and the corporate-memory bundle.
8. Render `AGNES_WORKSPACE.md` from `config/agnes_workspace_template.txt` —
   client-side template, three placeholders.

Errors render via `cli/error_render.py:render_error` with typed `kind` values
(`auth_failed`, `server_unreachable`, `partial_state`, `manifest_unauthorized`)
matching the rest of the CLI surface.

Task 18 will register `init_app` on the root Typer app.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer

from cli.client import api_get
from cli.config import save_config, save_token
from cli.error_render import render_error
from cli.lib.hooks import install_claude_hooks
from cli.lib.pull import PullResult, _override_server_env, run_pull


# Substring that flags an already-bootstrapped workspace. The current default
# CLAUDE.md template renders `# {{ instance.name }} — AI Data Analyst` so this
# appears in every server-rendered CLAUDE.md. Operators who use a custom admin
# template can override this via the `--force` flag.
_INIT_MARKER = "AI Data Analyst"


init_app = typer.Typer(help="Bootstrap an analyst workspace in this directory")


@init_app.callback(invoke_without_command=True)
def init(
    server_url: str = typer.Option(..., "--server-url", help="Agnes server URL"),
    token: str = typer.Option(..., "--token", help="Personal access token"),
    force: bool = typer.Option(False, "--force", help="Re-initialize an existing workspace"),
    workspace_str: Optional[str] = typer.Option(None, "--workspace", help="Target dir (default: cwd)"),
):
    """Bootstrap workspace: auth, CLAUDE.md, hooks, first pull, AGNES_WORKSPACE.md."""
    workspace = Path(workspace_str).resolve() if workspace_str else Path.cwd()
    server_url = server_url.rstrip("/")

    # ------------------------------------------------------------------
    # Step 1: detect an existing workspace.
    # ------------------------------------------------------------------
    claude_md = workspace / "CLAUDE.md"
    if claude_md.exists() and not force:
        try:
            existing = claude_md.read_text(encoding="utf-8")
        except OSError:
            existing = ""
        if _INIT_MARKER in existing:
            typer.echo(render_error(0, {"detail": {
                "kind": "partial_state",
                "hint": "Workspace already initialized. Re-run with --force to redo.",
            }}), err=True)
            raise typer.Exit(1)

    # ------------------------------------------------------------------
    # Step 2: verify the PAT via /api/catalog/tables.
    #
    # `api_get` reads server URL + token from env vars (`AGNES_SERVER`,
    # `AGNES_TOKEN`) via `cli.config`. Wrap the call in
    # `_override_server_env` so the explicit args take effect without
    # mutating the caller's environment permanently. Same mechanism as
    # `cli.lib.pull.run_pull`.
    # ------------------------------------------------------------------
    try:
        with _override_server_env(server_url, token):
            resp = api_get("/api/catalog/tables")
        if resp.status_code == 401:
            typer.echo(render_error(401, {"detail": {
                "kind": "auth_failed",
                "hint": f"Token expired or invalid — get a fresh one at {server_url}/setup?role=analyst",
            }}), err=True)
            raise typer.Exit(1)
        resp.raise_for_status()
    except typer.Exit:
        raise
    except Exception as exc:
        typer.echo(render_error(0, {"detail": {
            "kind": "server_unreachable",
            "hint": f"Cannot reach {server_url} — check network or server status",
            "message": str(exc),
        }}), err=True)
        raise typer.Exit(1)

    # ------------------------------------------------------------------
    # Step 3: save server URL + token to ~/.config/agnes/ so subsequent
    # invocations (including the SessionStart hook) read them by default.
    # `email=""` because the JWT carries it server-side; we don't decode
    # the token on the client.
    # ------------------------------------------------------------------
    save_config({"server": server_url})
    save_token(token, email="")

    # ------------------------------------------------------------------
    # Step 4: fetch the rendered CLAUDE.md from /api/welcome.
    # ------------------------------------------------------------------
    workspace.mkdir(parents=True, exist_ok=True)
    try:
        with _override_server_env(server_url, token):
            welcome_resp = api_get("/api/welcome", params={"server_url": server_url})
        welcome_resp.raise_for_status()
    except Exception as exc:
        typer.echo(render_error(0, {"detail": {
            "kind": "server_unreachable",
            "hint": "Failed to fetch CLAUDE.md from /api/welcome",
            "message": str(exc),
        }}), err=True)
        raise typer.Exit(1)
    welcome_content = welcome_resp.json().get("content", "")
    claude_md.write_text(welcome_content, encoding="utf-8")

    # ------------------------------------------------------------------
    # Step 5: default settings.json + install hooks.
    #
    # Seed first-run model + permissions only when the file is absent;
    # `install_claude_hooks` then merges SessionStart/End on top, leaving
    # any third-party keys/hooks intact. Re-running init (with or without
    # --force) is idempotent on settings.json.
    # ------------------------------------------------------------------
    settings_path = workspace / ".claude" / "settings.json"
    if not settings_path.exists():
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps(
            {"model": "sonnet", "permissions": {"allow": ["Read", "Bash", "Grep", "Glob"]}},
            indent=2,
        ))
    install_claude_hooks(workspace)

    # ------------------------------------------------------------------
    # Step 6: CLAUDE.local.md stub — only when absent. `--force` does NOT
    # overwrite; the operator's notes survive a re-init.
    # ------------------------------------------------------------------
    local_md = workspace / ".claude" / "CLAUDE.local.md"
    if not local_md.exists():
        local_md.parent.mkdir(parents=True, exist_ok=True)
        local_md.write_text(
            "# My Notes\n\nPersonal notes for this workspace. Uploaded on `agnes push`.\n",
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    # Step 7: first pull. `run_pull` records per-stage failures inside
    # `result.errors` rather than raising for transient issues, so any
    # exception escaping here is a programming error worth surfacing.
    # ------------------------------------------------------------------
    try:
        result: PullResult = run_pull(server_url, token, workspace)
    except Exception as exc:
        typer.echo(render_error(0, {"detail": {
            "kind": "manifest_unauthorized",
            "hint": "Initial pull failed — workspace partially set up",
            "message": str(exc),
        }}), err=True)
        raise typer.Exit(1)

    # `run_pull` records per-stage failures into `result.errors` and only
    # raises for programming errors. A manifest-stage failure here means
    # the analyst has a saved token + saved server URL but no parquets,
    # no DuckDB views — surface a typed error so the operator knows the
    # workspace is not actually queryable. Common cause: PAT validates
    # against /api/catalog/tables but lacks resource_grants for any tables.
    manifest_err = next((e for e in result.errors if e.get("stage") == "manifest"), None)
    if manifest_err:
        typer.echo(render_error(0, {"detail": {
            "kind": "manifest_unauthorized",
            "hint": "Manifest fetch failed — workspace partially set up. "
                    "Check that the PAT has resource_grants for at least one table.",
            "message": manifest_err.get("error", ""),
        }}), err=True)
        raise typer.Exit(1)

    # ------------------------------------------------------------------
    # Step 8: render AGNES_WORKSPACE.md from the static client-side
    # template. Three placeholders: created_at, server_url, workspace_path.
    # ------------------------------------------------------------------
    here = Path(__file__).parent
    template_path = here.parent.parent / "config" / "agnes_workspace_template.txt"
    if template_path.exists():
        template = template_path.read_text(encoding="utf-8")
    else:
        # Defensive fallback — the template ships with the repo so this
        # branch only fires on a broken install. Better than crashing.
        template = "# Agnes workspace\n\nCreated: {created_at}\nServer: {server_url}\n"
    workspace_md = (
        template
        .replace("{created_at}", datetime.now(timezone.utc).isoformat())
        .replace("{server_url}", server_url)
        .replace("{workspace_path}", str(workspace))
    )
    (workspace / "AGNES_WORKSPACE.md").write_text(workspace_md, encoding="utf-8")

    # ------------------------------------------------------------------
    # Final: human-readable summary.
    # ------------------------------------------------------------------
    typer.echo("Workspace ready.")
    typer.echo(f"  Server   : {server_url}")
    typer.echo(f"  Tables   : {result.tables_updated} synced ({result.parquets_total} total)")
    typer.echo(f"  Rules    : {result.rules_count}")
    typer.echo(f"  Workspace: {workspace}")
    typer.echo("")
    typer.echo("Try: agnes catalog")
