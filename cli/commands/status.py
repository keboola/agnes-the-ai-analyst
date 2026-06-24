"""`agnes status` — workspace status: initialized? data fresh? hooks active?

Server-health checks live under `agnes diagnose system` (see the
`agnes diagnose` group).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import typer


# Mirrors the dual-marker convention documented in cli/commands/init.py:
# `.claude/init-complete` is the authoritative sentinel written by every
# successful init (default OR Initial-Workspace-override mode); the legacy
# CLAUDE.md substring is kept as a fallback for pre-#259 workspaces. The
# sentinel-first ordering matters for override workspaces: a customer-
# supplied template body may legitimately omit the literal "AI Data
# Analyst" substring (the marker is hardcoded against the default Agnes
# template's `# {{ instance.name }} — AI Data Analyst` heading), and the
# legacy grep alone would then falsely report "Initialized: no" even
# when init wrote the sentinel and the workspace is functional.
_INIT_SENTINEL = Path(".claude") / "init-complete"
_INIT_MARKER = "AI Data Analyst"


status_app = typer.Typer(help="Show workspace status (initialized? data fresh? hooks active?)")


@status_app.callback(invoke_without_command=True)
def status(
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
):
    workspace = Path(os.environ.get("AGNES_LOCAL_DIR", ".")).resolve()

    initialized = (workspace / _INIT_SENTINEL).exists()
    if not initialized:
        claude_md = workspace / "CLAUDE.md"
        if claude_md.exists():
            try:
                initialized = _INIT_MARKER in claude_md.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                initialized = False

    parquet_dir = workspace / "server" / "parquet"
    parquets = list(parquet_dir.glob("*.parquet")) if parquet_dir.exists() else []

    db_path = workspace / "user" / "duckdb" / "analytics.duckdb"
    last_synced = None
    if db_path.exists():
        last_synced = datetime.fromtimestamp(db_path.stat().st_mtime, tz=timezone.utc).isoformat()

    # Sessions live in <projects_root>/<encoded-workspace_root>/ where Claude
    # Code writes them. Count what `agnes push` would scan — anchored on the
    # `workspace_root` config key (the same anchor push uses), so a status run
    # from any cwd reports the real workspace. 0 when unset.
    from cli.config import get_workspace_root
    from cli.lib.session_paths import list_session_files
    ws_root = get_workspace_root()
    session_count = len(list_session_files(Path(ws_root))) if ws_root else 0

    info = {
        "workspace": str(workspace),
        "initialized": initialized,
        "parquet_tables": len(parquets),
        "duckdb_exists": db_path.exists(),
        "last_synced": last_synced,
        "sessions_pending_upload": session_count,
    }

    if as_json:
        typer.echo(json.dumps(info, indent=2))
        return

    typer.echo(f"Workspace : {workspace}")
    typer.echo(f"Initialized: {'yes' if initialized else 'no'}")
    typer.echo(f"Parquets  : {info['parquet_tables']}")
    typer.echo(f"DuckDB    : {'yes' if info['duckdb_exists'] else 'no'}")
    typer.echo(f"Last sync : {last_synced or 'never'}")
    typer.echo(f"Pending uploads: {session_count} sessions")

    if not initialized:
        typer.echo("")
        typer.echo("Run `agnes init --server-url <URL> --token <PAT>` to bootstrap.")
