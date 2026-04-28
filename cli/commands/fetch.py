"""`da fetch` — materialize a filtered subset of a remote table locally (spec §4.2)."""

from __future__ import annotations
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import typer

from cli.snapshot_meta import (
    SnapshotMeta, write_meta, read_meta, snapshot_lock,
)
from cli.v2_client import api_post_json, api_post_arrow, V2ClientError

fetch_app = typer.Typer(
    help="Fetch a filtered subset of a remote table locally",
    context_settings={"allow_interspersed_args": True},
)


def _local_dir() -> Path:
    return Path(os.environ.get("DA_LOCAL_DIR", ".")).resolve()


def _print_estimate(d: dict) -> None:
    # `dict.get(k, default)` returns `default` only when k is missing; if k
    # maps to None (server returns None for non-BQ tables) the default doesn't
    # kick in. `or 0` covers both cases.
    typer.echo(f"  estimated_scan_bytes:   {(d.get('estimated_scan_bytes') or 0):>15,} bytes")
    typer.echo(f"  estimated_result_rows:  {(d.get('estimated_result_rows') or 0):>15,}")
    typer.echo(f"  estimated_result_bytes: {(d.get('estimated_result_bytes') or 0):>15,} bytes")
    typer.echo(f"  bq_cost_estimate_usd:   $ {(d.get('bq_cost_estimate_usd') or 0):.4f}")


@fetch_app.callback(invoke_without_command=True)
def fetch(
    ctx: typer.Context,
    table_id: str = typer.Argument(...),
    select: str = typer.Option(None, "--select", help="Comma-separated column list"),
    where: str = typer.Option(None, "--where", help="WHERE predicate (BQ flavor for remote tables)"),
    limit: int = typer.Option(None, "--limit"),
    order_by: str = typer.Option(None, "--order-by", help="Comma-separated"),
    as_name: str = typer.Option(None, "--as", help="Local snapshot name (default: <table_id>)"),
    estimate: bool = typer.Option(False, "--estimate", help="Run dry-run only, do not fetch"),
    no_estimate: bool = typer.Option(False, "--no-estimate", help="Skip the pre-fetch estimate"),
    force: bool = typer.Option(False, "--force", help="Overwrite existing snapshot of the same name"),
):
    """Fetch a filtered subset of a remote table locally."""
    if ctx.invoked_subcommand is not None:
        return

    name = as_name or table_id
    # Snapshot name lands in DuckDB CREATE VIEW as a quoted identifier; a `"`
    # in the name would break out and enable arbitrary SQL execution against
    # the user's local analytics.duckdb. Validate up-front with the same
    # regex used elsewhere for safe identifiers.
    import re as _re
    if not _re.match(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$", name):
        typer.echo(
            f"Error: snapshot name {name!r} is not a safe identifier. "
            f"Use letters, digits, and underscores; must start with a letter "
            f"or underscore; max 64 characters.",
            err=True,
        )
        raise typer.Exit(2)
    snap_dir = _local_dir() / "user" / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)

    # Build request
    req = {"table_id": table_id}
    if select:
        req["select"] = [c.strip() for c in select.split(",") if c.strip()]
    if where:
        req["where"] = where
    if limit:
        req["limit"] = int(limit)
    if order_by:
        req["order_by"] = [c.strip() for c in order_by.split(",") if c.strip()]

    # Estimate (always shown unless --no-estimate). The `--estimate` early
    # exit is OUTSIDE this block — `--estimate` is a cost-safety mechanism
    # ("dry-run only, do not fetch") whose guarantee must hold even when
    # the user also passes `--no-estimate` (silly combo; treat as dry-run
    # because the fetch-blocking semantics dominate).
    est = None
    if not no_estimate:
        try:
            est = api_post_json("/api/v2/scan/estimate", req)
        except V2ClientError as e:
            typer.echo(f"Error: estimate failed: {e}", err=True)
            raise typer.Exit(_exit_code_for(e))
        typer.echo(f"Estimate for {table_id}:")
        _print_estimate(est)
    if estimate:
        return

    # Snapshot existence check
    if not force and read_meta(snap_dir, name) is not None:
        existing = read_meta(snap_dir, name)
        typer.echo(
            f"Error: snapshot {name!r} already exists "
            f"(fetched {existing.fetched_at}, {existing.rows:,} rows). "
            f"Pass --force to overwrite, or 'da snapshot refresh {name}' to update in place.",
            err=True,
        )
        raise typer.Exit(6)

    # Fetch
    try:
        table = api_post_arrow("/api/v2/scan", req)
    except V2ClientError as e:
        typer.echo(f"Error: fetch failed: {e}", err=True)
        raise typer.Exit(_exit_code_for(e))

    # Install under flock
    parquet_path = snap_dir / f"{name}.parquet"
    with snapshot_lock(snap_dir):
        pq.write_table(table, parquet_path)
        # Register view in user analytics.duckdb
        local_db = _local_dir() / "user" / "duckdb" / "analytics.duckdb"
        local_db.parent.mkdir(parents=True, exist_ok=True)
        conn = duckdb.connect(str(local_db))
        try:
            safe_path = str(parquet_path).replace("'", "''")
            conn.execute(
                f"CREATE OR REPLACE VIEW \"{name}\" AS SELECT * FROM read_parquet('{safe_path}')"
            )
        finally:
            conn.close()

        # Compute hash + write meta
        result_hash = hashlib.md5(parquet_path.read_bytes()[:1_000_000]).hexdigest()
        now = datetime.now(timezone.utc).isoformat()
        meta = SnapshotMeta(
            name=name, table_id=table_id,
            select=req.get("select"), where=req.get("where"),
            limit=req.get("limit"), order_by=req.get("order_by"),
            fetched_at=now, effective_as_of=now,
            rows=int(table.num_rows),
            bytes_local=parquet_path.stat().st_size,
            estimated_scan_bytes_at_fetch=int(est.get("estimated_scan_bytes", 0)) if est is not None else 0,
            result_hash_md5=result_hash,
        )
        write_meta(snap_dir, meta)

    typer.echo(f"Fetched {table.num_rows:,} rows -> {name}")


def _exit_code_for(e: V2ClientError) -> int:
    if e.status_code == 400:
        # Inspect body for 'kind'
        body = e.body if isinstance(e.body, dict) else {}
        if body.get("error") == "validator_rejected":
            return 2
        return 2
    if e.status_code == 401:
        return 7
    if e.status_code == 403:
        return 8
    if e.status_code == 404:
        return 8  # treat unknown table as RBAC-equivalent
    if e.status_code == 429:
        return 3
    if e.status_code >= 500:
        return 5
    return 9
