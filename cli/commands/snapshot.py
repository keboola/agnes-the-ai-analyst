"""`agnes snapshot list/create/refresh/drop/prune` (spec §4.2)."""

from __future__ import annotations
import hashlib
import os
import json as json_lib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import duckdb
import httpx
import pyarrow.parquet as pq

from src.duckdb_conn import _open_duckdb
import typer

from cli.snapshot_meta import (
    list_snapshots, read_meta, write_meta, delete_snapshot,
    snapshot_lock, sweep_expired_snapshots, SnapshotMeta,
)
from cli.v2_client import api_post_arrow, api_post_json, V2ClientError

snapshot_app = typer.Typer(help="Manage local snapshots")


def _local_dir() -> Path:
    return Path(os.environ.get("AGNES_LOCAL_DIR", ".")).resolve()


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
    typer.echo(
        f"{'NAME':30s}  {'ROWS':>10s}  {'SIZE':>10s}  {'AGE':>10s}  "
        f"{'EXPIRES':>20s}  {'TABLE':30s}  WHERE"
    )
    now = datetime.now(timezone.utc)
    for s in sorted(snaps, key=lambda x: x.name):
        try:
            age = now - datetime.fromisoformat(s.fetched_at.replace("Z", "+00:00"))
            age_str = f"{age.days}d" if age.days else f"{int(age.total_seconds() // 3600)}h"
        except (ValueError, TypeError):
            age_str = "?"
        expires_str = _format_expires(s.expires_at, now)
        where = (s.where or "")[:40]
        typer.echo(
            f"{s.name:30s}  {s.rows:>10,}  {_format_size(s.bytes_local):>10s}  "
            f"{age_str:>10s}  {expires_str:>20s}  {s.table_id:30s}  {where}"
        )


def _format_expires(expires_at: Optional[str], now: datetime) -> str:
    """Render `expires_at` for `snapshot list`.

    None → "-" (no TTL). A past instant → "expired". A future instant → the
    remaining time until expiry (e.g. "6d", "12h").
    """
    if not expires_at:
        return "-"
    try:
        exp = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return "?"
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    remaining = exp - now
    if remaining.total_seconds() <= 0:
        return "expired"
    if remaining.days:
        return f"in {remaining.days}d"
    return f"in {int(remaining.total_seconds() // 3600)}h"


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
            conn = _open_duckdb(str(local_db))
            try:
                conn.execute(f'DROP VIEW IF EXISTS "{name}"')
            finally:
                conn.close()
    typer.echo(f"Dropped {name}")


@snapshot_app.command("refresh")
def refresh_cmd(
    name: str,
    where: str = typer.Option(None, "--where", help="Override stored WHERE"),
    ttl: str = typer.Option(
        None, "--ttl",
        help=(
            "Reset the snapshot's TTL (e.g. 7d / 24h / 90m), re-anchored to "
            "now. Omit to keep the existing expiry unchanged."
        ),
    ),
):
    """Re-fetch a snapshot using its stored fetch parameters (spec §4.2)."""
    snap_dir = _snap_dir()
    meta = read_meta(snap_dir, name)
    if meta is None:
        typer.echo(f"Error: snapshot {name!r} not found", err=True)
        raise typer.Exit(2)

    if ttl is not None:
        try:
            _parse_duration(ttl)
        except ValueError:
            typer.echo(
                f"Error: invalid --ttl {ttl!r}. Use a duration like 7d, 24h, or 90m.",
                err=True,
            )
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
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        # --ttl re-anchors the expiry to now; otherwise keep the stored one.
        expires_at = (
            (now_dt + _parse_duration(ttl)).isoformat() if ttl is not None
            else meta.expires_at
        )
        new_meta = SnapshotMeta(
            name=name, table_id=meta.table_id,
            select=req.get("select"), where=req.get("where"),
            limit=req.get("limit"), order_by=req.get("order_by"),
            fetched_at=now, effective_as_of=now,
            rows=new_rows, bytes_local=new_bytes,
            estimated_scan_bytes_at_fetch=meta.estimated_scan_bytes_at_fetch,
            result_hash_md5=new_hash,
            expires_at=expires_at,
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
    expired: bool = typer.Option(
        False, "--expired",
        help="Drop snapshots whose --ttl has elapsed (same sweep `agnes pull` runs).",
    ),
    dry_run: bool = typer.Option(False, "--dry-run"),
):
    """Drop snapshots matching predicates."""
    snap_dir = _snap_dir()

    # --expired delegates to the shared sweep helper so the manual command
    # and the lazy `agnes pull` sweep can never drift apart (#407). It's a
    # standalone selector — combining it with --older-than / --larger-than
    # would mix TTL-expiry with age/size predicates, so treat it exclusively.
    if expired:
        if dry_run:
            now = datetime.now(timezone.utc)
            matched = False
            for s in list_snapshots(snap_dir):
                if not s.expires_at:
                    continue
                try:
                    exp = datetime.fromisoformat(s.expires_at.replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    continue
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                if exp <= now:
                    matched = True
                    typer.echo(f"would drop: {s.name}  (expired {s.expires_at})")
            if not matched:
                typer.echo("(no matches)")
            return
        swept = sweep_expired_snapshots(snap_dir)
        for name in swept:
            typer.echo(f"dropped: {name}")
        if not swept:
            typer.echo("(no matches)")
        return

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


def _print_estimate(d: dict) -> None:
    # `dict.get(k, default)` returns `default` only when k is missing; if k
    # maps to None (server returns None for non-BQ tables) the default doesn't
    # kick in. `or 0` covers both cases.
    typer.echo(f"  estimated_scan_bytes:   {(d.get('estimated_scan_bytes') or 0):>15,} bytes")
    typer.echo(f"  estimated_result_rows:  {(d.get('estimated_result_rows') or 0):>15,}")
    typer.echo(f"  estimated_result_bytes: {(d.get('estimated_result_bytes') or 0):>15,} bytes")
    typer.echo(f"  bq_cost_estimate_usd:   $ {(d.get('bq_cost_estimate_usd') or 0):.4f}")


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


@snapshot_app.command("create")
def create_cmd(
    table_id: str = typer.Argument(...),
    select: str = typer.Option(None, "--select", help="Comma-separated column list"),
    where: str = typer.Option(None, "--where", help="WHERE predicate (BQ flavor for remote tables)"),
    limit: int = typer.Option(None, "--limit"),
    order_by: str = typer.Option(None, "--order-by", help="Comma-separated"),
    as_name: str = typer.Option(None, "--as", help="Local snapshot name (default: <table_id>)"),
    estimate: bool = typer.Option(False, "--estimate", help="Run dry-run only, do not fetch"),
    no_estimate: bool = typer.Option(False, "--no-estimate", help="Skip the pre-fetch estimate"),
    force: bool = typer.Option(False, "--force", help="Overwrite existing snapshot of the same name"),
    from_query: str = typer.Option(
        None, "--from-query",
        help=(
            "Materialize a snapshot from a raw SELECT executed remotely "
            "(BigQuery does the projection in the query — no --select/--where "
            "parsing). Mutually exclusive with --select/--where/--order-by. "
            "Backs `agnes query --remote --auto-snapshot`."
        ),
    ),
    ttl: str = typer.Option(
        None, "--ttl",
        help=(
            "Time-to-live, e.g. 7d / 24h / 90m. After it elapses the snapshot "
            "is removed by the lazy sweep on the next `agnes pull` (or "
            "`agnes snapshot prune --expired`). Omit for no expiry."
        ),
    ),
):
    """Create a snapshot — fetch a filtered subset of a remote table locally."""
    # Thin Typer wrapper → plain function so other code (e.g. the
    # `agnes query --remote --auto-snapshot` fallback in cli/commands/query.py)
    # can invoke the create logic directly without re-deriving Typer
    # OptionInfo defaults (#616).
    _create_snapshot(
        table_id=table_id, select=select, where=where, limit=limit,
        order_by=order_by, as_name=as_name, estimate=estimate,
        no_estimate=no_estimate, force=force, from_query=from_query, ttl=ttl,
    )


def _create_snapshot(
    *,
    table_id: str,
    select: Optional[str] = None,
    where: Optional[str] = None,
    limit: Optional[int] = None,
    order_by: Optional[str] = None,
    as_name: Optional[str] = None,
    estimate: bool = False,
    no_estimate: bool = False,
    force: bool = False,
    from_query: Optional[str] = None,
    ttl: Optional[str] = None,
    quiet: bool = False,
) -> None:
    """Create-snapshot implementation (plain function, no Typer types).

    ``quiet=True`` routes the final ``Fetched … rows`` success line to stderr
    instead of stdout — used by the ``agnes query --remote --auto-snapshot``
    fallback so the snapshot chatter doesn't pollute the query's stdout
    (which may be `--format json`)."""
    name = as_name or table_id

    # --from-query carries its own projection; reject the select/where path.
    if from_query is not None and any(x is not None for x in (select, where, order_by, limit)):
        typer.echo(
            "Error: --from-query is mutually exclusive with "
            "--select/--where/--order-by/--limit.",
            err=True,
        )
        raise typer.Exit(2)

    # Validate --ttl up-front (before any network call) so a typo fails fast.
    if ttl is not None:
        try:
            _parse_duration(ttl)
        except ValueError:
            typer.echo(
                f"Error: invalid --ttl {ttl!r}. Use a duration like 7d, 24h, or 90m.",
                err=True,
            )
            raise typer.Exit(2)
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

    # Guard: refuse to create snapshots before `agnes pull` has bootstrapped
    # the local DuckDB. Otherwise we'd open an empty DB and confuse later
    # `agnes pull` runs.
    #
    # `--estimate` is exempt: it's a server-side dry-run cost check that
    # never touches the local DuckDB, so it doesn't need the DB to exist
    # (and analysts use it pre-bootstrap to scope a fetch before deciding
    # to materialize).
    if not estimate:
        local_db = _local_dir() / "user" / "duckdb" / "analytics.duckdb"
        if not local_db.exists():
            typer.echo("Local DuckDB not found. Run: agnes pull first.", err=True)
            raise typer.Exit(1)

    snap_dir = _local_dir() / "user" / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)

    # Build request
    req = {"table_id": table_id}
    if from_query is not None:
        # Raw-SQL materialize path (#616): the query carries its own
        # projection, so the request is just {from_query, as}. The
        # select/where-based estimate doesn't apply to a raw query, so we
        # force-skip it below.
        req["from_query"] = from_query
        req["as"] = name
        no_estimate = True
    else:
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
        except httpx.HTTPError as e:
            # Connection refused / DNS / TLS / timeout — friendly render so
            # `agnes snapshot create … --estimate` in a pre-init dir (no
            # server configured, defaults to http://localhost:8000) prints
            # a hint instead of leaking an httpx traceback to stderr.
            typer.echo(
                f"Error: could not reach server ({e.__class__.__name__}). "
                f"Run `agnes init --server-url <url> --token <pat>` first.",
                err=True,
            )
            raise typer.Exit(7)
        typer.echo(f"Estimate for {table_id}:")
        _print_estimate(est)
    if estimate:
        return

    # Cheap existence pre-check (outside the lock) so we don't waste a BQ
    # scan on an obviously-redundant fetch. Authoritative re-check happens
    # under the lock below — necessary because between this check and the
    # write a concurrent `agnes snapshot create --as same_name` could create
    # the file.
    if not force and read_meta(snap_dir, name) is not None:
        existing = read_meta(snap_dir, name)
        typer.echo(
            f"Error: snapshot {name!r} already exists "
            f"(fetched {existing.fetched_at}, {existing.rows:,} rows). "
            f"Pass --force to overwrite, or 'agnes snapshot refresh {name}' to update in place.",
            err=True,
        )
        raise typer.Exit(6)

    # Fetch
    try:
        table = api_post_arrow("/api/v2/scan", req)
    except V2ClientError as e:
        typer.echo(f"Error: fetch failed: {e}", err=True)
        raise typer.Exit(_exit_code_for(e))

    # Install under flock — re-check existence here to close the TOCTOU
    # window between the early check above and this write.
    parquet_path = snap_dir / f"{name}.parquet"
    with snapshot_lock(snap_dir):
        if not force and read_meta(snap_dir, name) is not None:
            existing = read_meta(snap_dir, name)
            typer.echo(
                f"Error: snapshot {name!r} was created by a concurrent "
                f"`agnes snapshot create` (fetched {existing.fetched_at}, "
                f"{existing.rows:,} rows). Pass --force to overwrite.",
                err=True,
            )
            raise typer.Exit(6)
        pq.write_table(table, parquet_path)
        # Register view in user analytics.duckdb (already verified to exist
        # above — we still pass parents=True because the directory may have
        # been deleted between the guard and here in pathological cases).
        local_db.parent.mkdir(parents=True, exist_ok=True)
        conn = _open_duckdb(str(local_db))
        try:
            safe_path = str(parquet_path).replace("'", "''")
            conn.execute(
                f"CREATE OR REPLACE VIEW \"{name}\" AS SELECT * FROM read_parquet('{safe_path}')"
            )
        finally:
            conn.close()

        # Compute hash + write meta
        result_hash = hashlib.md5(parquet_path.read_bytes()[:1_000_000]).hexdigest()
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        expires_at = (now_dt + _parse_duration(ttl)).isoformat() if ttl else None
        meta = SnapshotMeta(
            name=name, table_id=table_id,
            select=req.get("select"), where=req.get("where"),
            limit=req.get("limit"), order_by=req.get("order_by"),
            fetched_at=now, effective_as_of=now,
            rows=int(table.num_rows),
            bytes_local=parquet_path.stat().st_size,
            estimated_scan_bytes_at_fetch=int(est.get("estimated_scan_bytes", 0)) if est is not None else 0,
            result_hash_md5=result_hash,
            expires_at=expires_at,
        )
        write_meta(snap_dir, meta)

    if ttl:
        typer.echo(f"Fetched {table.num_rows:,} rows -> {name} (expires {expires_at})", err=quiet)
    else:
        typer.echo(f"Fetched {table.num_rows:,} rows -> {name}", err=quiet)


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
