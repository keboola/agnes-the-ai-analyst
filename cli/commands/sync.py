"""Sync commands — da sync."""

import json
import os
from pathlib import Path

import typer
from rich.progress import Progress, SpinnerColumn, TextColumn

from cli.client import api_get, api_post, stream_download
from cli.config import get_sync_state, save_sync_state

sync_app = typer.Typer(help="Data synchronization")


def _local_data_dir() -> Path:
    return Path(os.environ.get("DA_LOCAL_DIR", "."))


@sync_app.callback(invoke_without_command=True)
def sync(
    table: str = typer.Option(None, "--table", help="Sync specific table only"),
    upload_only: bool = typer.Option(False, "--upload-only", help="Only upload sessions/artifacts"),
    docs_only: bool = typer.Option(False, "--docs-only", help="Only sync documentation"),
    as_json: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Sync data between server and local machine."""
    if upload_only:
        _upload(as_json)
        return

    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}")) as progress:
        # 1. Get manifest
        task = progress.add_task("Fetching manifest...", total=None)
        try:
            resp = api_get("/api/sync/manifest")
            resp.raise_for_status()
            manifest = resp.json()
        except Exception as e:
            typer.echo(f"Failed to fetch manifest: {e}", err=True)
            raise typer.Exit(1)

        server_tables = manifest.get("tables", {})
        local_state = get_sync_state()
        local_tables = local_state.get("tables", {})

        # 2. Determine what to download
        to_download = []
        for tid, info in server_tables.items():
            if table and tid != table:
                continue
            if docs_only:
                continue
            local_hash = local_tables.get(tid, {}).get("hash", "")
            if info.get("hash", "") != local_hash:
                to_download.append(tid)

        progress.update(task, description=f"Found {len(to_download)} tables to sync")

        # 3. Download parquets
        local_dir = _local_data_dir()
        parquet_dir = local_dir / "server" / "parquet"
        parquet_dir.mkdir(parents=True, exist_ok=True)

        results = {"downloaded": [], "skipped": [], "errors": []}
        for tid in to_download:
            progress.update(task, description=f"Downloading {tid}...")
            target = parquet_dir / f"{tid}.parquet"
            try:
                stream_download(f"/api/data/{tid}/download", str(target))
                local_tables[tid] = {
                    "hash": server_tables[tid].get("hash", ""),
                    "rows": server_tables[tid].get("rows", 0),
                    "size_bytes": server_tables[tid].get("size_bytes", 0),
                }
                results["downloaded"].append(tid)
            except Exception as e:
                results["errors"].append({"table": tid, "error": str(e)})

        # 4. Save local state
        from datetime import datetime, timezone
        local_state["tables"] = local_tables
        local_state["last_sync"] = datetime.now(timezone.utc).isoformat()
        save_sync_state(local_state)

        # 5. Rebuild DuckDB views
        if results["downloaded"]:
            progress.update(task, description="Rebuilding DuckDB views...")
            _rebuild_duckdb_views(local_dir, parquet_dir)

        progress.update(task, description="Sync complete")

    # Output
    skipped = len(server_tables) - len(to_download)
    if as_json:
        typer.echo(json.dumps(results, indent=2))
    else:
        typer.echo(f"Downloaded: {len(results['downloaded'])} tables")
        typer.echo(f"Skipped (unchanged): {skipped}")
        if results["errors"]:
            typer.echo(f"Errors: {len(results['errors'])}")
            for err in results["errors"]:
                typer.echo(f"  {err['table']}: {err['error']}")


def _rebuild_duckdb_views(local_dir: Path, parquet_dir: Path):
    """Recreate DuckDB views from downloaded parquets. Preserve user tables."""
    import duckdb

    db_path = local_dir / "user" / "duckdb" / "analytics.duckdb"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(db_path))

    # Get existing user-created tables (not views)
    try:
        existing_tables = {
            row[0] for row in
            conn.execute("SELECT table_name FROM information_schema.tables WHERE table_type='BASE TABLE'").fetchall()
        }
    except Exception:
        existing_tables = set()

    # Drop all views
    try:
        views = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_type='VIEW'"
        ).fetchall()
        for (view_name,) in views:
            conn.execute(f'DROP VIEW IF EXISTS "{view_name}"')
    except Exception:
        pass

    # Create views for each parquet file
    for pq_file in parquet_dir.rglob("*.parquet"):
        view_name = pq_file.stem
        if view_name in existing_tables:
            continue  # don't shadow user tables
        abs_path = str(pq_file.resolve())
        conn.execute(f"CREATE VIEW \"{view_name}\" AS SELECT * FROM read_parquet('{abs_path}')")

    conn.close()


def _upload(as_json: bool):
    """Upload sessions and CLAUDE.local.md to server."""
    local_dir = _local_data_dir()
    results = {"sessions": 0, "local_md": False}

    # Upload sessions
    sessions_dir = local_dir / "user" / "sessions"
    if sessions_dir.exists():
        for f in sessions_dir.glob("*.jsonl"):
            try:
                with open(f, "rb") as fh:
                    resp = api_post("/api/upload/sessions", files={"file": (f.name, fh)})
                    if resp.status_code == 200:
                        results["sessions"] += 1
            except Exception:
                pass

    # Upload CLAUDE.local.md
    local_md = local_dir / ".claude" / "CLAUDE.local.md"
    if local_md.exists():
        content = local_md.read_text(encoding="utf-8")
        try:
            resp = api_post("/api/upload/local-md", json={"content": content})
            if resp.status_code == 200:
                results["local_md"] = True
        except Exception:
            pass

    if as_json:
        typer.echo(json.dumps(results, indent=2))
    else:
        typer.echo(f"Uploaded {results['sessions']} sessions")
        if results["local_md"]:
            typer.echo("Uploaded CLAUDE.local.md")
