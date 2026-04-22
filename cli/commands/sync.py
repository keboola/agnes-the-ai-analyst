"""Sync commands — da sync."""

import hashlib
import json
import os
from pathlib import Path

import typer
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)

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
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show what would be synced without downloading, uploading, or writing local state.",
    ),
):
    """Sync data between server and local machine."""
    if upload_only:
        _upload(as_json, dry_run=dry_run)
        return

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
    ) as progress:
        # 1. Get manifest — indeterminate spinner (total unknown until manifest lands)
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
            server_hash = info.get("hash", "")
            # Download if: hashes differ, or no local copy, or hash is empty (not computed)
            if server_hash != local_hash or tid not in local_tables or not server_hash:
                to_download.append(tid)

        # Switch the bar from indeterminate to "X/N" progress once we know the total.
        progress.update(
            task,
            description=f"Found {len(to_download)} tables to sync",
            total=len(to_download) or None,
            completed=0,
        )

        # 3. Dry-run short-circuit — report what would happen, touch nothing on disk.
        if dry_run:
            progress.update(task, description="Dry run — nothing will be downloaded")
            _print_dry_run_plan(to_download, server_tables, len(server_tables), as_json)
            return

        # 4. Download parquets
        local_dir = _local_data_dir()
        parquet_dir = local_dir / "server" / "parquet"
        parquet_dir.mkdir(parents=True, exist_ok=True)

        results = {"downloaded": [], "skipped": [], "errors": []}
        total = len(to_download)
        for idx, tid in enumerate(to_download, start=1):
            progress.update(task, description=f"[{idx}/{total}] Downloading {tid}...")
            target = parquet_dir / f"{tid}.parquet"
            expected_hash = server_tables[tid].get("hash", "")
            try:
                stream_download(f"/api/data/{tid}/download", str(target))
                # Integrity check against the manifest hash (server uses MD5
                # over the parquet — see app/api/sync.py:_file_hash). A
                # structural PAR1 check is kept as a fallback for when the
                # manifest hash is empty (legacy snapshots).
                if expected_hash:
                    actual_hash = _md5_file(target)
                    if actual_hash != expected_hash:
                        target.unlink(missing_ok=True)
                        raise ValueError(
                            f"hash mismatch: expected {expected_hash[:12]}…, got {actual_hash[:12]}…"
                        )
                elif not _is_valid_parquet(target):
                    target.unlink(missing_ok=True)
                    raise ValueError(
                        "downloaded file is not a valid parquet (missing PAR1 magic bytes)"
                    )
                local_tables[tid] = {
                    "hash": expected_hash,
                    "rows": server_tables[tid].get("rows", 0),
                    "size_bytes": server_tables[tid].get("size_bytes", 0),
                }
                results["downloaded"].append(tid)
            except Exception as e:
                results["errors"].append({"table": tid, "error": str(e)})
            progress.advance(task, 1)

        # 5. Save local state
        from datetime import datetime, timezone
        local_state["tables"] = local_tables
        local_state["last_sync"] = datetime.now(timezone.utc).isoformat()
        save_sync_state(local_state)

        # 6. Rebuild DuckDB views
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


def _print_dry_run_plan(
    to_download: list[str],
    server_tables: dict,
    total_tables: int,
    as_json: bool,
) -> None:
    """Render the dry-run plan for the download flow (no disk writes).

    Pairs table IDs with their manifest `size_bytes` / `rows` so the operator
    can judge cost before committing to the real sync.
    """
    total_bytes = sum(server_tables.get(tid, {}).get("size_bytes", 0) or 0 for tid in to_download)
    plan = [
        {
            "table": tid,
            "rows": server_tables.get(tid, {}).get("rows", 0) or 0,
            "size_bytes": server_tables.get(tid, {}).get("size_bytes", 0) or 0,
        }
        for tid in to_download
    ]
    if as_json:
        typer.echo(json.dumps(
            {
                "dry_run": True,
                "would_download": plan,
                "summary": {
                    "tables_total": total_tables,
                    "tables_to_download": len(to_download),
                    "tables_skipped_unchanged": total_tables - len(to_download),
                    "bytes_total": total_bytes,
                },
            },
            indent=2,
        ))
        return

    typer.echo(f"Dry run — would download {len(to_download)} tables ({_fmt_bytes(total_bytes)})")
    typer.echo(f"Skipped (unchanged): {total_tables - len(to_download)}")
    for row in plan:
        typer.echo(f"  {row['table']}  rows={row['rows']}  size={_fmt_bytes(row['size_bytes'])}")


def _fmt_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        n /= 1024
        if n < 1024:
            return f"{n:.1f} {unit}"
    return f"{n:.1f} PiB"


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

    # Create views for each parquet file. One broken file (corrupt download,
    # partial write left over from a previous sync, …) must not abort the
    # whole rebuild — skip it with a warning and keep going.
    skipped_broken: list[str] = []
    for pq_file in parquet_dir.rglob("*.parquet"):
        view_name = pq_file.stem
        if view_name in existing_tables:
            continue  # don't shadow user tables
        if not _is_valid_parquet(pq_file):
            skipped_broken.append(view_name)
            continue
        abs_path = str(pq_file.resolve())
        try:
            conn.execute(f"CREATE VIEW \"{view_name}\" AS SELECT * FROM read_parquet('{abs_path}')")
        except duckdb.Error:
            skipped_broken.append(view_name)

    conn.close()

    if skipped_broken:
        typer.echo(
            f"Warning: skipped {len(skipped_broken)} broken parquet file(s) during view rebuild:",
            err=True,
        )
        for name in skipped_broken:
            typer.echo(f"  - {name}.parquet", err=True)


def _md5_file(path: Path) -> str:
    """MD5 of a file, same chunking as app/api/sync.py:_file_hash so the
    client-side verification matches the manifest hash byte-for-byte."""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_valid_parquet(path: Path) -> bool:
    """Cheap structural check — parquet files begin and end with `PAR1`.

    Used as a fallback when the manifest has no hash (legacy snapshots) and
    during view rebuild to skip obviously-broken files. Does not guarantee
    the footer is well-formed — that's DuckDB's job at CREATE VIEW time.
    """
    try:
        size = path.stat().st_size
        if size < 8:
            return False
        with open(path, "rb") as f:
            head = f.read(4)
            f.seek(-4, 2)
            tail = f.read(4)
        return head == b"PAR1" and tail == b"PAR1"
    except OSError:
        return False


def _upload(as_json: bool, dry_run: bool = False):
    """Upload sessions and CLAUDE.local.md to server.

    When `dry_run=True`, enumerate what would be uploaded without hitting the
    API or mutating anything on disk.
    """
    local_dir = _local_data_dir()
    sessions_dir = local_dir / "user" / "sessions"
    local_md = local_dir / ".claude" / "CLAUDE.local.md"

    if dry_run:
        session_files = sorted(str(f) for f in sessions_dir.glob("*.jsonl")) if sessions_dir.exists() else []
        plan = {
            "dry_run": True,
            "would_upload": {
                "sessions": session_files,
                "local_md": str(local_md) if local_md.exists() else None,
            },
            "summary": {
                "sessions_count": len(session_files),
                "local_md_present": local_md.exists(),
            },
        }
        if as_json:
            typer.echo(json.dumps(plan, indent=2))
            return
        typer.echo(f"Dry run — would upload {len(session_files)} session file(s)")
        for f in session_files:
            typer.echo(f"  {f}")
        if local_md.exists():
            typer.echo(f"Would upload CLAUDE.local.md  ({local_md})")
        else:
            typer.echo("No CLAUDE.local.md to upload")
        return

    results = {"sessions": 0, "local_md": False}

    # Upload sessions
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
