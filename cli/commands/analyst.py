"""Analyst bootstrap commands — da analyst setup, da analyst status."""

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import typer

_SAFE_IDENTIFIER = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")

analyst_app = typer.Typer(help="Analyst workspace bootstrap and status")

# ---------------------------------------------------------------------------
# Helper: detect existing workspace
# ---------------------------------------------------------------------------

_CLAUDE_MD_MARKER = "AI Data Analyst"


def _detect_existing_project(workspace: Path) -> bool:
    """Return True if CLAUDE.md with the analyst identifier already exists."""
    claude_md = workspace / "CLAUDE.md"
    if claude_md.exists():
        content = claude_md.read_text(encoding="utf-8")
        return _CLAUDE_MD_MARKER in content
    return False


# ---------------------------------------------------------------------------
# Helper: connect to instance (health check + authenticate)
# ---------------------------------------------------------------------------

def _connect_to_instance(server_url: str) -> str:
    """Health-check the server, prompt for credentials, save config, return JWT."""
    import httpx
    from cli.config import save_config, save_token

    server_url = server_url.rstrip("/")

    # Health check
    try:
        resp = httpx.get(f"{server_url}/api/health", timeout=10.0)
        resp.raise_for_status()
    except Exception as e:
        typer.echo(f"Cannot reach {server_url}: {e}", err=True)
        raise typer.Exit(1)

    typer.echo(f"Connected to {server_url}")

    # Authenticate
    email = typer.prompt("Email")
    password = typer.prompt("Password", hide_input=True)

    try:
        resp = httpx.post(
            f"{server_url}/auth/token",
            json={"email": email, "password": password},
            timeout=15.0,
        )
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            typer.echo("Authentication failed: invalid credentials", err=True)
        elif e.response.status_code == 403:
            typer.echo("Authentication failed: account disabled or forbidden", err=True)
        else:
            typer.echo(f"Authentication failed: HTTP {e.response.status_code}", err=True)
        raise typer.Exit(1)
    except httpx.TimeoutException:
        typer.echo(f"Authentication failed: connection timeout to {server_url}", err=True)
        raise typer.Exit(1)
    except Exception as e:
        typer.echo(f"Authentication failed: {e}", err=True)
        raise typer.Exit(1)

    token = data.get("access_token")
    if not token:
        typer.echo("Authentication failed: server response missing access_token", err=True)
        raise typer.Exit(1)
    role = data.get("role", "analyst")

    save_config({"server": server_url})
    save_token(token, email, role)
    typer.echo(f"Authenticated as {email} (role: {role})")
    return token


# ---------------------------------------------------------------------------
# Helper: create workspace directory structure
# ---------------------------------------------------------------------------

def _create_workspace(workspace: Path) -> None:
    """Create the analyst workspace directory layout."""
    dirs = [
        workspace / "data" / "parquet",
        workspace / "data" / "duckdb",
        workspace / "data" / "metadata",
        workspace / "user" / "artifacts",
        workspace / "user" / "sessions",
        workspace / ".claude",
    ]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Helper: download metadata
# ---------------------------------------------------------------------------

def _download_metadata(workspace: Path, server_url: str, token: str) -> None:
    """Fetch catalog tables and metrics from the server; save as JSON files."""
    import httpx

    server_url = server_url.rstrip("/")
    headers = {"Authorization": f"Bearer {token}"}
    metadata_dir = workspace / "data" / "metadata"

    # Catalog tables
    try:
        resp = httpx.get(f"{server_url}/api/catalog/tables", headers=headers, timeout=30.0)
        resp.raise_for_status()
        tables = resp.json()
    except Exception as e:
        typer.echo(f"Warning: could not fetch catalog tables: {e}", err=True)
        tables = []

    (metadata_dir / "schema.json").write_text(
        json.dumps(tables, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # Metrics
    try:
        resp = httpx.get(f"{server_url}/api/metrics", headers=headers, timeout=30.0)
        resp.raise_for_status()
        metrics = resp.json()
    except Exception as e:
        typer.echo(f"Warning: could not fetch metrics: {e}", err=True)
        metrics = []

    (metadata_dir / "metrics.json").write_text(
        json.dumps(metrics, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # Write last_sync timestamp
    last_sync = {"synced_at": datetime.now(timezone.utc).isoformat()}
    (metadata_dir / "last_sync.json").write_text(
        json.dumps(last_sync, indent=2), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Helper: download parquet data
# ---------------------------------------------------------------------------

def _download_data(workspace: Path, server_url: str, token: str) -> int:
    """Stream parquets for each registered table. Returns count of files downloaded."""
    import httpx

    server_url = server_url.rstrip("/")
    headers = {"Authorization": f"Bearer {token}"}
    parquet_dir = workspace / "data" / "parquet"

    # Fetch manifest to know which tables exist
    try:
        resp = httpx.get(f"{server_url}/api/sync/manifest", headers=headers, timeout=30.0)
        resp.raise_for_status()
        manifest = resp.json()
    except Exception as e:
        typer.echo(f"Warning: could not fetch data manifest: {e}", err=True)
        return 0

    tables = manifest.get("tables", {})
    downloaded = 0

    for table_id in tables:
        target = parquet_dir / f"{table_id}.parquet"
        if target.exists():
            typer.echo(f"  Skipping {table_id} (already exists)")
            continue

        try:
            with httpx.stream(
                "GET",
                f"{server_url}/api/data/{table_id}/download",
                headers=headers,
                timeout=300.0,
            ) as stream_resp:
                stream_resp.raise_for_status()
                with open(target, "wb") as fh:
                    for chunk in stream_resp.iter_bytes(chunk_size=65536):
                        fh.write(chunk)
            downloaded += 1
            typer.echo(f"  Downloaded {table_id}")
        except Exception as e:
            typer.echo(f"  Warning: could not download {table_id}: {e}", err=True)
            if target.exists():
                target.unlink()

    return downloaded


# ---------------------------------------------------------------------------
# Helper: initialise DuckDB
# ---------------------------------------------------------------------------

def _initialize_duckdb(workspace: Path) -> int:
    """Create DuckDB views over parquets. Returns total row count across all views."""
    import duckdb

    parquet_dir = workspace / "data" / "parquet"
    db_path = workspace / "data" / "duckdb" / "analytics.duckdb"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = duckdb.connect(str(db_path))
    total_rows = 0

    parquet_dir_resolved = parquet_dir.resolve()
    for pq_file in parquet_dir.glob("*.parquet"):
        view_name = pq_file.stem
        # Validate path is within the expected parquet directory (no path traversal)
        try:
            pq_resolved = pq_file.resolve()
            pq_resolved.relative_to(parquet_dir_resolved)
        except ValueError:
            typer.echo(f"  Warning: Skipping {pq_file.name}: path traversal detected", err=True)
            continue
        # Validate view name is a safe SQL identifier
        if not _SAFE_IDENTIFIER.match(view_name):
            typer.echo(f"  Warning: Skipping {pq_file.name}: unsafe view name", err=True)
            continue
        abs_path = str(pq_resolved)
        safe_path = abs_path.replace("'", "''")
        try:
            conn.execute(f'DROP VIEW IF EXISTS "{view_name}"')
            conn.execute(
                f"CREATE VIEW \"{view_name}\" AS SELECT * FROM read_parquet('{safe_path}')"
            )
            count = conn.execute(f'SELECT count(*) FROM "{view_name}"').fetchone()[0]
            total_rows += count
        except Exception as e:
            typer.echo(f"  Warning: could not create view for {view_name}: {e}", err=True)

    conn.close()
    return total_rows


# ---------------------------------------------------------------------------
# Helper: resolve instance name
# ---------------------------------------------------------------------------

def _get_instance_name(server_url: str, token: str) -> str:
    """Retrieve instance name from /api/health, fall back to hostname."""
    import httpx

    server_url = server_url.rstrip("/")
    headers = {"Authorization": f"Bearer {token}"}

    try:
        resp = httpx.get(f"{server_url}/api/health", headers=headers, timeout=10.0)
        if resp.status_code == 200:
            data = resp.json()
            name = data.get("instance_name") or data.get("name")
            if name:
                return name
    except Exception:
        pass

    # Fall back to hostname extracted from URL
    parsed = urlparse(server_url)
    return parsed.hostname or "AI Data Analyst"


# ---------------------------------------------------------------------------
# Helper: install SessionStart/End hooks into a Claude settings file
# ---------------------------------------------------------------------------

def _install_claude_hooks(settings_path: Path) -> None:
    """Add SessionStart/SessionEnd hooks calling `da sync` to a Claude settings file.

    Idempotent: replaces our prior `da sync` entries (matched by command substring
    `da sync`) but preserves anyone else's hooks. Creates the file when missing.

    The settings file is workspace-level (`<workspace>/.claude/settings.json`) so
    the hooks only fire in this analyst workspace, not in unrelated Claude Code
    sessions on the same machine.
    """
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    if settings_path.exists():
        try:
            cfg = json.loads(settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            typer.echo(
                f"Warning: {settings_path} is not valid JSON; skipping hook install.",
                err=True,
            )
            return
    else:
        cfg = {}

    hooks = cfg.setdefault("hooks", {})

    def _replace_or_add(event: str, command: str) -> None:
        existing = hooks.setdefault(event, [])
        # Drop any prior entry whose every command is a `da sync` invocation.
        # Third-party entries (PreToolUse: echo hi) and mixed entries are left alone.
        for entry in list(existing):
            entry_cmds = [h.get("command", "") for h in entry.get("hooks", [])]
            if entry_cmds and all("da sync" in c for c in entry_cmds):
                existing.remove(entry)
        existing.append({"hooks": [{"type": "command", "command": command}]})

    _replace_or_add("SessionStart", "da sync --quiet 2>/dev/null || true")
    _replace_or_add("SessionEnd",   "da sync --upload-only --quiet 2>/dev/null || true")

    settings_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Helper: generate CLAUDE.md from template
# ---------------------------------------------------------------------------

def _generate_claude_md(
    workspace: Path,
    instance_name: str,
    server_url: str,
    sync_interval: str,
) -> None:
    """Write CLAUDE.md from the template; create CLAUDE.local.md if absent."""
    # Locate template relative to this file (../../config/claude_md_template.txt)
    here = Path(__file__).parent
    template_path = here.parent.parent / "config" / "claude_md_template.txt"

    if template_path.exists():
        template = template_path.read_text(encoding="utf-8")
    else:
        # Fallback minimal template
        template = (
            "# {instance_name} — AI Data Analyst\n\n"
            "This workspace is connected to {server_url}.\n\n"
            "- Data on the server refreshes every {sync_interval}\n"
        )

    content = (
        template
        .replace("{instance_name}", instance_name)
        .replace("{server_url}", server_url)
        .replace("{sync_interval}", sync_interval)
    )

    (workspace / "CLAUDE.md").write_text(content, encoding="utf-8")

    # .claude/CLAUDE.local.md — never overwrite if it already exists
    local_md = workspace / ".claude" / "CLAUDE.local.md"
    if not local_md.exists():
        local_md.write_text(
            "# My Notes\n\n"
            "Personal notes for this workspace. Uploaded to the server on `da sync --upload-only`.\n",
            encoding="utf-8",
        )

    settings_path = workspace / ".claude" / "settings.json"
    if not settings_path.exists():
        # First-run defaults: model + permissions. _install_claude_hooks below
        # will merge in the SessionStart/End hooks on top of these.
        settings = {"model": "sonnet", "permissions": {"allow": ["Read", "Bash", "Grep", "Glob"]}}
        settings_path.write_text(json.dumps(settings, indent=2))

    _install_claude_hooks(settings_path)


# ---------------------------------------------------------------------------
# Helper: data freshness check (for returning-session detection)
# ---------------------------------------------------------------------------

def _check_data_freshness(workspace: Path) -> str:
    """Return 'fresh', 'stale' (>24 h old), or 'missing'."""
    last_sync_file = workspace / "data" / "metadata" / "last_sync.json"
    if not last_sync_file.exists():
        return "missing"

    try:
        data = json.loads(last_sync_file.read_text(encoding="utf-8"))
        synced_at_str = data.get("synced_at", "")
        if not synced_at_str:
            return "missing"
        synced_at = datetime.fromisoformat(synced_at_str)
        age_hours = (datetime.now(timezone.utc) - synced_at).total_seconds() / 3600
        return "stale" if age_hours > 24 else "fresh"
    except Exception:
        return "missing"


# ---------------------------------------------------------------------------
# Command: da analyst setup
# ---------------------------------------------------------------------------

@analyst_app.command()
def setup(
    server_url: str = typer.Option(..., "--server-url", help="URL of the AI Data Analyst server"),
    force: bool = typer.Option(False, "--force", help="Re-initialise even if workspace already exists"),
    sync_interval: str = typer.Option("1 hour", "--sync-interval", help="Data refresh interval shown in CLAUDE.md"),
    workspace_dir: Optional[str] = typer.Option(None, "--workspace", help="Workspace directory (default: current dir)"),
):
    """Bootstrap a new analyst workspace from a remote server."""
    workspace = Path(workspace_dir).resolve() if workspace_dir else Path.cwd()

    # 1. Detect existing project
    if _detect_existing_project(workspace) and not force:
        typer.echo(
            "Existing analyst workspace detected. Use --force to re-initialise.",
            err=True,
        )
        raise typer.Exit(1)

    typer.echo(f"Setting up analyst workspace in: {workspace}")

    # 2. Connect to instance
    token = _connect_to_instance(server_url)

    # 3. Create workspace directory structure
    typer.echo("Creating workspace directories...")
    _create_workspace(workspace)

    # 4. Download metadata
    typer.echo("Downloading metadata...")
    _download_metadata(workspace, server_url, token)

    # 5. Download data
    typer.echo("Downloading data...")
    n_downloaded = _download_data(workspace, server_url, token)

    # 6. Initialise DuckDB
    typer.echo("Initialising DuckDB views...")
    total_rows = _initialize_duckdb(workspace)

    # 7. Generate CLAUDE.md
    typer.echo("Generating CLAUDE.md...")
    instance_name = _get_instance_name(server_url, token)
    _generate_claude_md(workspace, instance_name, server_url, sync_interval)

    # 8. Summary
    typer.echo("")
    typer.echo("Setup complete!")
    typer.echo(f"  Instance : {instance_name}")
    typer.echo(f"  Server   : {server_url}")
    typer.echo(f"  Tables   : {n_downloaded} downloaded, {total_rows} total rows")
    typer.echo(f"  Workspace: {workspace}")
    typer.echo(f"  Hooks    : SessionStart/End installed in {workspace}/.claude/settings.json")
    typer.echo("")
    typer.echo("Next steps:")
    typer.echo("  da sync          — refresh data")
    typer.echo("  da metrics list  — explore available metrics")


# ---------------------------------------------------------------------------
# Command: da analyst status
# ---------------------------------------------------------------------------

@analyst_app.command()
def status(
    workspace_dir: Optional[str] = typer.Option(None, "--workspace", help="Workspace directory (default: current dir)"),
    as_json: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Show workspace status and data freshness for returning sessions."""
    workspace = Path(workspace_dir).resolve() if workspace_dir else Path.cwd()

    exists = _detect_existing_project(workspace)
    freshness = _check_data_freshness(workspace)

    # Count parquet files
    parquet_dir = workspace / "data" / "parquet"
    parquet_count = len(list(parquet_dir.glob("*.parquet"))) if parquet_dir.exists() else 0

    # Last sync timestamp
    last_sync_file = workspace / "data" / "metadata" / "last_sync.json"
    last_sync = "never"
    if last_sync_file.exists():
        try:
            data = json.loads(last_sync_file.read_text(encoding="utf-8"))
            last_sync = data.get("synced_at", "never")
        except Exception:
            pass

    info = {
        "workspace": str(workspace),
        "initialized": exists,
        "freshness": freshness,
        "parquet_tables": parquet_count,
        "last_sync": last_sync,
    }

    if as_json:
        typer.echo(json.dumps(info, indent=2))
        return

    typer.echo(f"Workspace : {workspace}")
    typer.echo(f"Initialized: {'yes' if exists else 'no'}")
    typer.echo(f"Data freshness: {freshness}")
    typer.echo(f"Parquet tables: {parquet_count}")
    typer.echo(f"Last sync: {last_sync}")

    if freshness == "stale":
        typer.echo("")
        typer.echo("Data is stale (>24 h). Run: da sync")
    elif freshness == "missing":
        typer.echo("")
        typer.echo("No data found. Run: da analyst setup --server-url <url>")
