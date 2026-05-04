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


_INIT_MARKER = "AI Data Analyst"


status_app = typer.Typer(help="Show workspace status (initialized? data fresh? hooks active?)")


@status_app.callback(invoke_without_command=True)
def status(
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
):
    workspace = Path(os.environ.get("AGNES_LOCAL_DIR", ".")).resolve()

    initialized = False
    claude_md = workspace / "CLAUDE.md"
    if claude_md.exists():
        initialized = _INIT_MARKER in claude_md.read_text(encoding="utf-8")

    parquet_dir = workspace / "server" / "parquet"
    parquets = list(parquet_dir.glob("*.parquet")) if parquet_dir.exists() else []

    db_path = workspace / "user" / "duckdb" / "analytics.duckdb"
    last_synced = None
    if db_path.exists():
        last_synced = datetime.fromtimestamp(db_path.stat().st_mtime, tz=timezone.utc).isoformat()

    sessions_dir = workspace / "user" / "sessions"
    session_count = len(list(sessions_dir.glob("*.jsonl"))) if sessions_dir.exists() else 0

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
