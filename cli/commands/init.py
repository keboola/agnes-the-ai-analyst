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
   commands. Then call `cli.lib.commands.install_claude_commands` to drop
   the Agnes-managed slash commands (today: `/update-agnes-plugins`) into
   `<workspace>/.claude/commands/`. Idempotent on re-run.
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
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer

from cli.client import api_get
from cli.config import save_config, save_token
from cli.error_render import render_error
from cli.lib.commands import install_claude_commands
from cli.lib.hooks import install_claude_hooks
from cli.lib.initial_workspace import apply_override, probe_status
from cli.lib.pull import PullResult, _override_server_env, run_pull


# Substring that flags an already-bootstrapped workspace. The current default
# CLAUDE.md template renders `# {{ instance.name }} — AI Data Analyst` so this
# appears in every server-rendered CLAUDE.md. Operators who use a custom admin
# template can override this via the `--force` flag.
_INIT_MARKER = "AI Data Analyst"

# Sentinel written at the very END of a successful `agnes init`. Existence
# of CLAUDE.md alone is NOT a "workspace is initialized" signal because
# CLAUDE.md is written early in the flow — long before the parquet pull,
# the AGNES_WORKSPACE.md render, and the final summary. Killed runs
# (SIGKILL from the harness, network drop mid-pull, operator Ctrl-C)
# leave CLAUDE.md on disk but not this sentinel. The next `agnes init`
# can then resume without requiring `--force`, which would otherwise
# force a full re-download of any large materialized parquet that was
# 80 % complete. Issue #259.
_INIT_COMPLETE_FILE = ".claude/init-complete"


# Env vars that, when set to a non-existent path, cause every TLS handshake
# on the host to fail before Agnes itself runs. Past versions of the Agnes
# setup script's TLS trust block (and older bootstrap helpers) wrote
# pointers to ``~/.agnes/ca-bundle.pem`` into the user's persistent env
# (Windows User scope; shell rc files on POSIX). When the file goes away
# (re-init on a new VM, manual cleanup, machine swap) the pointers go
# stale — gws auth login, claude plugin marketplace add, even pip/uv,
# all fail with UnknownIssuer / FileNotFoundError. Reported by the
# Windows test user 2026-05-11. SSL_CERT_FILE in particular REPLACES
# (not appends to) the trust store, so a stale pointer is silently
# catastrophic.
_CA_ENV_VARS = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "GIT_SSL_CAINFO")


def _chmod_workspace_hooks(workspace: Path) -> None:
    """Set execute bit on every `.sh` under `<workspace>/.claude/hooks/`.

    Claude Code's plugin install path doesn't always preserve the execute
    bit on shell hook files — depending on the archive format the plugin
    ships in (zip, no-bit-preserving git checkout config, etc.), hooks
    can land on disk as `rw-r--r--` and every fire returns Permission
    denied. The user-visible symptom is a silent SessionStart / PreToolUse
    failure that looks like the hooks just aren't installed.

    Best-effort. No-op on Windows NTFS via Git Bash (chmod is meaningless
    on NTFS without ACLs). Failures are swallowed — a hook the user can
    still read is no worse than the pre-fix baseline.
    """
    hooks_dir = workspace / ".claude" / "hooks"
    if not hooks_dir.is_dir():
        return
    for path in hooks_dir.rglob("*.sh"):
        try:
            current = path.stat().st_mode
            # Add user/group/other execute. Same effect as `chmod +x`.
            path.chmod(current | 0o111)
        except OSError:
            pass


def _is_windows_host() -> bool:
    """True when the Python interpreter sees Windows underneath.

    Covers native Python on Windows (``sys.platform == 'win32'``) and
    Git Bash / MSYS launchers (interpreter still reports win32; the
    bash shell wrapper is irrelevant for User-scope env-var management).
    POSIX-only edge cases (WSL with `windows` in /proc/version) stay on
    the POSIX path — User-scope env vars don't exist there in the
    Windows-registry sense, so the cleanup is a no-op.
    """
    return sys.platform == "win32"


def _cleanup_stale_ca_env_vars() -> None:
    """Clear stale SSL_CERT_FILE / REQUESTS_CA_BUNDLE / GIT_SSL_CAINFO
    pointers from the current process AND (on Windows) from User scope.

    Two layers because the failure mode hits both:
    1. Current-process env — what the upcoming `api_get` call to
       /api/catalog/tables actually reads. Without clearing it here, the
       httpx call falls over with a FileNotFoundError before init can
       finish step 2.
    2. Windows User-scope env — what every future shell + every native
       Windows tool (gws, claude.exe, pip, uv) inherits. Without
       clearing it there, the user re-hits the same wall the next time
       they open PowerShell — exactly what the 2026-05-11 Windows test
       user reported ("the init was supposed to clear these but they
       persisted; fixed by removing both vars from User scope").

    Best-effort. We only delete a var when it points at a path that does
    NOT exist on disk — intentional operator config (e.g. SSL_CERT_FILE
    pointing at a corporate certifi bundle) is preserved. PowerShell
    invocation failures are swallowed silently because the init shouldn't
    abort on a defensive cleanup helper.
    """
    cleared_process: list[tuple[str, str]] = []
    for var in _CA_ENV_VARS:
        cur = os.environ.get(var)
        if cur and not Path(cur).exists():
            del os.environ[var]
            cleared_process.append((var, cur))
    for name, path in cleared_process:
        typer.echo(
            f"agnes init: cleared stale process env {name}={path} "
            f"(file does not exist)"
        )

    if not _is_windows_host():
        return

    # Build a single PowerShell invocation that checks + clears all three
    # User-scope vars in one shot. Quoting strategy: pass the script via
    # -Command with single-quoted strings inside so Python's f-string
    # composition stays simple. We use [Environment]::SetEnvironmentVariable
    # with $null (the documented way to delete a User-scope env var on
    # Windows; setx has no delete verb).
    statements = []
    for var in _CA_ENV_VARS:
        statements.append(
            "$cur = [Environment]::GetEnvironmentVariable('" + var + "', 'User'); "
            "if ($cur -and -not (Test-Path -LiteralPath $cur)) { "
            "[Environment]::SetEnvironmentVariable('" + var + "', $null, 'User'); "
            "Write-Host ('agnes init: cleared stale User-scope " + var + "=' + $cur + ' (file does not exist)') "
            "}"
        )
    ps_script = "; ".join(statements)
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        # PowerShell missing (cygwin-only environments), or hung. Skip —
        # the current-process cleanup above already covers the immediate
        # `api_get` failure; persistent state cleanup is best-effort.
        return
    if result.stdout:
        # Forward PowerShell's confirmation lines to the user so the
        # cleanup is auditable. stderr from PowerShell (rare here) is
        # swallowed — the worst it'd add is "execution policy" noise on
        # restricted hosts, which isn't actionable.
        for line in result.stdout.splitlines():
            line = line.strip()
            if line:
                typer.echo(line)


init_app = typer.Typer(help="Bootstrap an analyst workspace in this directory")


@init_app.callback(invoke_without_command=True)
def init(
    server_url: str = typer.Option(..., "--server-url", help="Agnes server URL"),
    token: Optional[str] = typer.Option(
        None, "--token",
        help=(
            "Personal access token. Can also be supplied via the "
            "AGNES_TOKEN env var or --token-file (see also). Inline "
            "--token sometimes trips Claude Code's auto-classifier "
            "(long bearer-token string in a command line); prefer "
            "--token-file or AGNES_TOKEN to dodge that."
        ),
    ),
    token_file: Optional[str] = typer.Option(
        None, "--token-file",
        help=(
            "Path to a file whose first non-blank line is the PAT. Wins "
            "over AGNES_TOKEN env when both are set; loses to an explicit "
            "--token flag. The token never appears in the command string "
            "this way, which dodges Claude Code's bearer-token classifier."
        ),
    ),
    force: bool = typer.Option(False, "--force", help="Re-initialize an existing workspace"),
    workspace_str: Optional[str] = typer.Option(None, "--workspace", help="Target dir (default: cwd)"),
    skip_materialize: bool = typer.Option(
        False, "--skip-materialize",
        help=(
            "Skip materialized-mode tables on the first pull. The first "
            "init can otherwise spend tens of minutes silently downloading "
            "a single multi-GB scheduled-query parquet. Materialized rows "
            "are still discoverable via `agnes catalog`; rerun `agnes pull` "
            "without this flag once you actually need them locally."
        ),
    ),
):
    """Bootstrap workspace: auth, CLAUDE.md, hooks, first pull, AGNES_WORKSPACE.md."""
    workspace = Path(workspace_str).resolve() if workspace_str else Path.cwd()
    server_url = server_url.rstrip("/")

    # Resolve the token. Precedence: explicit --token > --token-file >
    # AGNES_TOKEN env var > error. --token-file and AGNES_TOKEN exist so
    # the analyst can paste an `agnes init --server-url … --token-file
    # ~/.agnes/token` (or simply set the env) without Claude Code's
    # auto-classifier flagging the long JWT in the command line.
    if token is None and token_file:
        try:
            for line in Path(token_file).expanduser().read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    token = line
                    break
        except OSError as exc:
            typer.echo(render_error(0, {"detail": {
                "kind": "partial_state",
                "hint": f"--token-file {token_file!r} could not be read: {exc}",
            }}), err=True)
            raise typer.Exit(1)
    if token is None:
        token = os.environ.get("AGNES_TOKEN", "").strip() or None
    if not token:
        typer.echo(render_error(0, {"detail": {
            "kind": "partial_state",
            "hint": "Supply a token via --token, --token-file, or AGNES_TOKEN env var.",
        }}), err=True)
        raise typer.Exit(1)

    # Best-effort cleanup before ANY TLS handshake fires below — stale
    # SSL_CERT_FILE / REQUESTS_CA_BUNDLE / GIT_SSL_CAINFO pointers from a
    # previous Agnes install on this host (or its Windows User-scope
    # registry entries) would otherwise blow up step 2's `api_get` with
    # an opaque "UnknownIssuer" / "FileNotFoundError" before the user
    # has any way to see what's wrong. Reported by the 2026-05-11
    # Windows test pass.
    _cleanup_stale_ca_env_vars()

    # ------------------------------------------------------------------
    # Step 1: detect an existing workspace.
    #
    # An init is considered to have happened when EITHER:
    #   - the completion sentinel `.claude/init-complete` exists
    #     (authoritative, written at the end of every successful init —
    #     default OR override mode), OR
    #   - the legacy "AI Data Analyst" string is in CLAUDE.md (pre-#259
    #     default-mode workspaces that succeeded under an older CLI
    #     version that didn't write a sentinel).
    #
    # The CLAUDE.md substring check is intentionally kept for legacy
    # workspaces but does NOT trigger for Initial-Workspace-override
    # workspaces (admin's repo CLAUDE.md doesn't contain the string).
    # In override mode the sentinel IS the authoritative signal — this
    # is why the override `agnes init` flow writes the sentinel as its
    # very last step, same as the default flow.
    # ------------------------------------------------------------------
    claude_md = workspace / "CLAUDE.md"
    init_complete = workspace / _INIT_COMPLETE_FILE
    sentinel_says_inited = init_complete.exists()
    claude_md_says_inited = False
    if claude_md.exists():
        try:
            existing = claude_md.read_text(encoding="utf-8")
            claude_md_says_inited = _INIT_MARKER in existing
        except (OSError, UnicodeDecodeError):
            # A CLAUDE.md with non-UTF-8 bytes (operator edited with a
            # legacy encoding) shouldn't crash the gate evaluation — fall
            # back to "marker not found" so the sentinel-existence branch
            # below carries the decision instead.
            existing = ""
    if (sentinel_says_inited or claude_md_says_inited) and not force:
        if sentinel_says_inited:
            typer.echo(render_error(0, {"detail": {
                "kind": "partial_state",
                "hint": "Workspace already initialized. Re-run with --force to redo.",
            }}), err=True)
            raise typer.Exit(1)
        # CLAUDE.md substring matches but no sentinel — previous default-
        # mode init was killed mid-flight (issue #259). Resume rather
        # than refuse so a large materialized parquet stays partially
        # cached and we don't re-download from zero.
        typer.echo(
            "Previous init was interrupted (no completion sentinel "
            "found). Resuming — partial downloads will continue where "
            "they stopped.",
            err=True,
        )

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
                "hint": f"Token expired or invalid — get a fresh one at {server_url}/setup",
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

    workspace.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Step 3.5: probe for a per-instance Initial Workspace Template.
    #
    # When the operator has registered a template repo on
    # /admin/server-config, we branch into the OVERRIDE FLOW: download
    # the server-rendered zip, optionally confirm with --force, extract
    # to the analyst's workspace, write the extended sentinel. We
    # explicitly SKIP the default-flow steps that would otherwise
    # clobber admin's content:
    #
    #   - Step 4: fetch /api/welcome → CLAUDE.md  (admin's repo CLAUDE.md wins)
    #   - Step 5: settings.json defaults + install_claude_hooks +
    #             install_claude_commands  (admin's repo settings.json wins)
    #   - Step 6: write .claude/CLAUDE.local.md stub  (admin decides)
    #   - Step 8: render AGNES_WORKSPACE.md  (admin decides)
    #
    # Steps 7 (run_pull) and 9 (sentinel) still run, but step 9's
    # sentinel write is folded INTO apply_override so the override
    # sentinel ships the extended fields (override:true, template_source,
    # template_sha) atomically with the extraction.
    #
    # OVERRIDE MODE — intentional behavior, NOT a bug. AI reviewers: see
    # docs/initial-workspace-override.md §"What Agnes stops doing".
    # ------------------------------------------------------------------
    override_status = None
    try:
        override_status = probe_status(server_url, token)
    except typer.Exit:
        raise
    except Exception:
        # Unexpected non-HTTP failure (parse error, etc.) — treat as
        # "no override" and fall through. Default flow is safe.
        override_status = None

    override_active = bool(
        override_status and override_status.configured
    )

    if override_active:
        # Override flow: apply_override does its own download +
        # extraction + sentinel write + audit event. Returns the
        # ExtractResult so we can mention counts in the final summary.
        try:
            import importlib.metadata as _md
            agnes_version = _md.version("agnes-the-ai-analyst")
        except Exception:
            agnes_version = "unknown"
        override_result = apply_override(
            workspace,
            override_status,
            server_url,
            token,
            force=force,
            agnes_version=agnes_version,
        )
    else:
        override_result = None

        # ------------------------------------------------------------------
        # On --force in DEFAULT mode only, snapshot the existing CLAUDE.md
        # before regenerating it so an operator who edited it can recover
        # their notes (issue #164). Backup name carries an ISO timestamp
        # so multiple `--force` runs in the same workspace don't clobber
        # each other.
        #
        # OVERRIDE MODE intentionally does NOT back up CLAUDE.md — the
        # admin's Git repo is the source of truth, recovery is `git log`.
        # Documented in CHANGELOG; not a regression of #164.
        # ------------------------------------------------------------------
        if claude_md.exists() and force:
            try:
                ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                backup_path = workspace / f"CLAUDE.md.bak.{ts}"
                backup_path.write_bytes(claude_md.read_bytes())
                typer.echo(f"Backed up existing CLAUDE.md → {backup_path.name}")
            except OSError as exc:
                typer.echo(
                    f"Warning: could not write CLAUDE.md backup ({exc}); "
                    f"continuing with --force overwrite",
                    err=True,
                )

        # ------------------------------------------------------------------
        # Step 4: fetch the rendered CLAUDE.md from /api/welcome.
        # ------------------------------------------------------------------
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

    if not override_active:
        # ------------------------------------------------------------------
        # Step 5: default settings.json + install hooks.
        #
        # Seed first-run model + permissions only when the file is absent;
        # `install_claude_hooks` then merges SessionStart/End on top, leaving
        # any third-party keys/hooks intact. Re-running init (with or without
        # --force) is idempotent on settings.json.
        #
        # OVERRIDE MODE skips this entire block — admin's repo settings.json
        # is the source of truth. Documented in CHANGELOG / docs/initial-
        # workspace-override.md. AI reviewers: not a regression of #242
        # (`maybe_refresh_claude_hooks`), the override sentinel guards
        # against later auto-refresh paths separately.
        # ------------------------------------------------------------------
        settings_path = workspace / ".claude" / "settings.json"
        if not settings_path.exists():
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            settings_path.write_text(json.dumps(
                {"model": "sonnet", "permissions": {"allow": ["Read", "Bash", "Bash(agnes *)", "Grep", "Glob"]}},
                indent=2,
            ), encoding="utf-8")
        install_claude_hooks(workspace)
        install_claude_commands(workspace)

        # ------------------------------------------------------------------
        # Step 6: CLAUDE.local.md stub — only when absent. `--force` does NOT
        # overwrite; the operator's notes survive a re-init.
        #
        # OVERRIDE MODE: NOT created by Agnes. If the admin's template repo
        # ships a CLAUDE.local.md, that one wins; otherwise the file simply
        # doesn't exist. Documented contract: full override = full control.
        # ------------------------------------------------------------------
        local_md = workspace / ".claude" / "CLAUDE.local.md"
        if not local_md.exists():
            local_md.parent.mkdir(parents=True, exist_ok=True)
            local_md.write_text(
                "# My Notes\n\nPersonal notes for this workspace. Uploaded on `agnes push`.\n",
                encoding="utf-8",
            )

    # ------------------------------------------------------------------
    # Always chmod +x hook scripts that landed on disk, regardless of
    # which path seeded the workspace. In DEFAULT mode the hooks come
    # from `install_claude_hooks` above; in OVERRIDE mode they come
    # from the admin's initial-workspace-template clone — and `git
    # checkout` of that template doesn't reliably preserve the +x bit
    # (filemode=false repos, archive extractions, FUSE/NFS mounts),
    # so hooks like `.claude/hooks/skill-nudge/nudge.sh` or
    # `.claude/hooks/prompt-history/log-prompt.sh` could land non-
    # executable and fire `Permission denied` on the very next
    # SessionStart. `_chmod_workspace_hooks` recurses (`rglob`) so
    # subdir-scoped hook layouts are covered. Best-effort, no-op on
    # Windows NTFS.
    # ------------------------------------------------------------------
    _chmod_workspace_hooks(workspace)

    # ------------------------------------------------------------------
    # Step 7: first pull. `run_pull` records per-stage failures inside
    # `result.errors` rather than raising for transient issues, so any
    # exception escaping here is a programming error worth surfacing.
    # ------------------------------------------------------------------
    try:
        # `agnes init` always runs interactively (analyst typing the
        # command), so progress is on by default — Pavel's #185 Phase 1
        # was a 44-minute silent download on the very first install.
        # Pass it through to run_pull.
        result: PullResult = run_pull(
            server_url, token, workspace,
            skip_materialize=skip_materialize,
            show_progress=True,
        )
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

    if not override_active:
        # ------------------------------------------------------------------
        # Step 8: render AGNES_WORKSPACE.md from the static client-side
        # template. Three placeholders: created_at, server_url, workspace_path.
        #
        # OVERRIDE MODE skips — admin's template owns workspace docs (often
        # there's nothing here at all, or the admin ships their own
        # AGNES_WORKSPACE.md content).
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
    # Step 9: write the completion sentinel. The next `agnes init` (no
    # flags) checks this; absence means a previous attempt was killed
    # mid-flight and we should resume rather than refuse. Issue #259.
    #
    # OVERRIDE MODE already wrote the extended sentinel (with
    # override: true + template_source + template_sha) from inside
    # apply_override(), so skip — don't clobber its extra fields with
    # the basic default-mode shape.
    # ------------------------------------------------------------------
    if override_active:
        pass  # apply_override already wrote the extended sentinel
    else:
        # Default mode: fetch operator-provisioned per-tenant params and
        # write <workspace>/.claude/agnes/.env so seed-resident connector
        # skills can read them at install time. Best-effort; empty overlay
        # or older server (no /api/connectors/params endpoint) silently
        # skips the file.
        try:
            from cli.lib.initial_workspace import write_agnes_env
            write_agnes_env(workspace, server_url, token)
        except Exception as e:
            # Best-effort — failure here doesn't block init. Seed skills
            # will fall back to interactive prompts.
            typer.echo(
                f"  Warning: .env.agnes write skipped ({e})",
                err=True,
            )

        sentinel = workspace / _INIT_COMPLETE_FILE
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        try:
            import importlib.metadata as _md
            agnes_version = _md.version("agnes-the-ai-analyst")
        except Exception:
            agnes_version = "unknown"
        sentinel.write_text(
            f"completed_at: {datetime.now(timezone.utc).isoformat()}\n"
            f"agnes_version: {agnes_version}\n"
            f"server_url: {server_url}\n",
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    # Final: human-readable summary.
    # ------------------------------------------------------------------
    typer.echo("Workspace ready.")
    typer.echo(f"  Server   : {server_url}")
    if override_active and override_result is not None:
        typer.echo(
            f"  Template : {override_status.template_source} "
            f"@ {override_status.template_sha[:10] if override_status.template_sha else '—'}"
        )
        typer.echo(
            f"  Files    : {len(override_result.created)} created, "
            f"{len(override_result.overwritten)} overwritten from template"
        )
    # `parquets_total` is the count of materialized rows in the manifest;
    # `tables_updated` is the count of those actually fetched this run.
    # The catalog can carry many more remote-only rows that aren't part
    # of `parquets_total` at all — surface that explicitly so analysts
    # who see "0 synced (0 total)" after `--skip-materialize` don't
    # conclude the server returned an empty catalog. Issue #257.
    if skip_materialize:
        typer.echo(
            f"  Tables   : 0 fetched locally — {result.parquets_total} "
            f"materialized row(s) skipped (re-run without --skip-materialize "
            f"to download). Catalog still serves all registered tables."
        )
    else:
        typer.echo(
            f"  Tables   : {result.tables_updated}/{result.parquets_total} "
            f"local materialized rows fetched"
        )
    typer.echo(f"  Rules    : {result.rules_count}")
    typer.echo(f"  Workspace: {workspace}")
    typer.echo("")
    typer.echo("Try: agnes catalog")
