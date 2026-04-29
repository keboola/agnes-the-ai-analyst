"""`da snapshot list/refresh/drop/prune` (spec §4.2)."""

from __future__ import annotations
import hashlib
import os
import json as json_lib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pyarrow.parquet as pq
import typer

from cli.snapshot_meta import (
    list_snapshots, read_meta, write_meta, delete_snapshot,
    snapshot_lock, SnapshotMeta,
)
from cli.v2_client import api_post_arrow, V2ClientError

snapshot_app = typer.Typer(help="Manage local snapshots")


def _local_dir() -> Path:
    return Path(os.environ.get("DA_LOCAL_DIR", ".")).resolve()


def _snap_dir() -> Path:
    return _local_dir() / "user" / "snapshots"


def _format_size(n: int) -> str:
    val = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if val < 1024 or unit == "TB":
            return f"{val:.1f} {unit}"
        val /= 1024
    return f"{val:.1f} TB"


@snapshot_app.command("list")
def list_cmd(
    json: bool = typer.Option(False, "--json"),
):
    """List local snapshots."""
    snaps = list_snapshots(_snap_dir())
    if json:
        typer.echo(json_lib.dumps([s.__dict__ for s in snaps], indent=2))
        return
    if not snaps:
        typer.echo("(no snapshots)")
        return
    typer.echo(f"{'NAME':30s}  {'ROWS':>10s}  {'SIZE':>10s}  {'AGE':>10s}  {'TABLE':30s}  WHERE")
    now = datetime.now(timezone.utc)
    for s in sorted(snaps, key=lambda x: x.name):
        try:
            age = now - datetime.fromisoformat(s.fetched_at.replace("Z", "+00:00"))
            age_str = f"{age.days}d" if age.days else f"{int(age.total_seconds() // 3600)}h"
        except (ValueError, TypeError):
            age_str = "?"
        where = (s.where or "")[:40]
        typer.echo(
            f"{s.name:30s}  {s.rows:>10,}  {_format_size(s.bytes_local):>10s}  "
            f"{age_str:>10s}  {s.table_id:30s}  {where}"
        )


@snapshot_app.command("drop")
def drop_cmd(name: str):
    """Delete a snapshot."""
    snap_dir = _snap_dir()
    if read_meta(snap_dir, name) is None:
        typer.echo(f"Error: snapshot {name!r} not found", err=True)
        raise typer.Exit(2)

    with snapshot_lock(snap_dir):
        delete_snapshot(snap_dir, name)
        # Also drop the view from user analytics DB
        local_db = _local_dir() / "user" / "duckdb" / "analytics.duckdb"
        if local_db.exists():
            conn = duckdb.connect(str(local_db))
            try:
                conn.execute(f'DROP VIEW IF EXISTS "{name}"')
            finally:
                conn.close()
    typer.echo(f"Dropped {name}")


@snapshot_app.command("refresh")
def refresh_cmd(
    name: str,
    where: str = typer.Option(None, "--where", help="Override stored WHERE"),
):
    """Re-fetch a snapshot using its stored fetch parameters (spec §4.2)."""
    snap_dir = _snap_dir()
    meta = read_meta(snap_dir, name)
    if meta is None:
        typer.echo(f"Error: snapshot {name!r} not found", err=True)
        raise typer.Exit(2)

    req = {
        "table_id": meta.table_id,
        "select": meta.select,
        "where": where if where else meta.where,
        "limit": meta.limit,
        "order_by": meta.order_by,
    }
    try:
        table = api_post_arrow("/api/v2/scan", req)
    except V2ClientError as e:
        typer.echo(f"Error: refresh failed: {e}", err=True)
        raise typer.Exit(5 if e.status_code >= 500 else 8 if e.status_code == 403 else 2)

    parquet_path = snap_dir / f"{name}.parquet"
    with snapshot_lock(snap_dir):
        pq.write_table(table, parquet_path)
        new_hash = hashlib.md5(parquet_path.read_bytes()[:1_000_000]).hexdigest()
        identical = new_hash == meta.result_hash_md5
        old_rows = meta.rows
        old_bytes = meta.bytes_local
        new_rows = int(table.num_rows)
        new_bytes = parquet_path.stat().st_size
        now = datetime.now(timezone.utc).isoformat()
        new_meta = SnapshotMeta(
            name=name, table_id=meta.table_id,
            select=req.get("select"), where=req.get("where"),
            limit=req.get("limit"), order_by=req.get("order_by"),
            fetched_at=now, effective_as_of=now,
            rows=new_rows, bytes_local=new_bytes,
            estimated_scan_bytes_at_fetch=meta.estimated_scan_bytes_at_fetch,
            result_hash_md5=new_hash,
        )
        write_meta(snap_dir, new_meta)

    typer.echo(f"Refreshed {name}")
    typer.echo(f"  rows:           {old_rows:>10,}  ->  {new_rows:>10,}  ({new_rows - old_rows:+,})")
    typer.echo(f"  bytes_local:    {_format_size(old_bytes)}  ->  {_format_size(new_bytes)}")
    typer.echo(f"  effective_as_of:{meta.effective_as_of}  ->  {now}")
    typer.echo(f"  identical:      {'yes' if identical else 'no'}")


@snapshot_app.command("prune")
def prune_cmd(
    older_than: str = typer.Option(None, "--older-than", help="e.g. 7d, 24h"),
    larger_than: str = typer.Option(None, "--larger-than", help="e.g. 1g, 500m"),
    dry_run: bool = typer.Option(False, "--dry-run"),
):
    """Drop snapshots matching predicates."""
    snap_dir = _snap_dir()
    snaps = list_snapshots(snap_dir)

    matches = []
    for s in snaps:
        ok = True
        if older_than:
            try:
                age = datetime.now(timezone.utc) - datetime.fromisoformat(s.fetched_at.replace("Z", "+00:00"))
                if age < _parse_duration(older_than):
                    ok = False
            except (ValueError, TypeError):
                ok = False
        if larger_than and s.bytes_local < _parse_size(larger_than):
            ok = False
        if ok:
            matches.append(s)

    for s in matches:
        if dry_run:
            typer.echo(f"would drop: {s.name}  ({_format_size(s.bytes_local)}, {s.fetched_at})")
        else:
            with snapshot_lock(snap_dir):
                delete_snapshot(snap_dir, s.name)
            typer.echo(f"dropped: {s.name}")
    if not matches:
        typer.echo("(no matches)")


def _parse_duration(s: str) -> timedelta:
    s = s.strip().lower()
    if s.endswith("d"):
        return timedelta(days=int(s[:-1]))
    if s.endswith("h"):
        return timedelta(hours=int(s[:-1]))
    if s.endswith("m"):
        return timedelta(minutes=int(s[:-1]))
    raise ValueError(f"unknown duration: {s!r}")


def _parse_size(s: str) -> int:
    s = s.strip().lower()
    multipliers = {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "t": 1024 ** 4}
    if s[-1] in multipliers:
        return int(float(s[:-1]) * multipliers[s[-1]])
    return int(s)
