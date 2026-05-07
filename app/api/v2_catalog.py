"""GET /api/v2/catalog — list tables visible to caller (spec §3.1)."""

from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
from fastapi import APIRouter, Depends
import duckdb

from app.auth.dependencies import get_current_user, _get_db
from app.utils import get_data_dir as _get_data_dir
from src.rbac import can_access_table
from src.repositories.table_registry import TableRegistryRepository
from app.api.v2_cache import TTLCache
from app.api._metadata_models import MetadataRequest, TableMetadata
from src.identifier_validation import validate_quoted_identifier

router = APIRouter(prefix="/api/v2", tags=["v2"])

# Global cache of the raw table_registry rows. RBAC is enforced PER REQUEST
# against this list, mirroring v2_schema.py / v2_sample.py — caching the
# RBAC-filtered payload per user used to leave revoked users seeing tables
# for up to TTL after a permission flip. Cache is single-keyed; the TTL
# matches the documented `api.catalog_cache_ttl_seconds` default at
# `config/instance.yaml.example`. The config knob isn't wired through yet
# (same status as schema/sample caches), so changing it in instance.yaml is
# a no-op — tracked separately.
_table_rows_cache = TTLCache(maxsize=1, ttl_seconds=300)
_TABLE_ROWS_KEY = "all"

# Per-table cached TableMetadata. 15-min TTL — long enough to amortise
# across an analyst session, short enough that a freshly-registered
# remote table shows real numbers within a coffee break (the cache-bust
# path in `invalidate_for_table` accelerates this for the common admin-
# verifies-registration flow).
_metadata_cache = TTLCache(maxsize=512, ttl_seconds=900)


def _metadata_provider_for(source_type: str):
    """Lazy-import dispatch for source-specific metadata providers.

    Lazy because connector modules are heavy (BQ extension, google-cloud
    client, etc.) and a Keboola-only deployment shouldn't pay the BQ
    import cost. Returns ``None`` for unknown source types — the caller
    treats that as "no metadata enrichment available" and falls through.
    """
    if source_type == "bigquery":
        from connectors.bigquery import metadata as m
        return m.fetch
    if source_type == "keboola":
        from connectors.keboola import metadata as m
        return m.fetch
    return None


def _build_metadata_request(row: dict) -> MetadataRequest | None:
    """Construct a validated MetadataRequest from a registry row.

    Pre-validates the identifiers via `validate_quoted_identifier` before
    constructing the request — providers can then interpolate
    `req.bucket` / `req.source_table` into SQL/URL paths without
    re-checking. Returns ``None`` when validation fails; provider is not
    dispatched for that row.
    """
    bucket = row.get("bucket") or ""
    source_table = row.get("source_table") or row.get("id") or ""
    if not bucket or not source_table:
        return None
    if not (validate_quoted_identifier(bucket, "bucket")
            and validate_quoted_identifier(source_table, "source_table")):
        return None
    return MetadataRequest(
        table_id=row["id"], bucket=bucket, source_table=source_table,
    )


def _flavor_for(source_type: str) -> str:
    return "bigquery" if source_type == "bigquery" else "duckdb"


def _examples_for(source_type: str) -> list[str]:
    if source_type == "bigquery":
        return [
            "event_date > DATE '2026-01-01'",
            "country_code = 'CZ' AND platform = 'web'",
        ]
    return []


def _fetch_hint(table_id: str, source_type: str) -> str:
    if source_type == "bigquery":
        return f"agnes snapshot create {table_id} --select <cols> --where '<BQ predicate>' --limit <N>"
    return "already local — query directly via `agnes query`"


# Coarse size buckets for `rough_size_hint`. Boundaries chosen so an analyst
# Claude can decide tool by inspection: anything `large` or worse implies
# `agnes snapshot create` over `agnes query --remote`. Numbers reflect the
# default `bq_max_scan_bytes` 5 GiB ceiling — at "large" you're already at
# half the per-query gate and a naive `--remote` is likely to refuse.
_SIZE_BUCKETS = (
    (10 * 2**20, "small"),     # ≤10 MiB
    (100 * 2**20, "small"),    # ≤100 MiB still small (analyst-laptop scale)
    (1 * 2**30, "medium"),     # ≤1 GiB
    (10 * 2**30, "large"),     # ≤10 GiB
)


def _bucket_size(byte_count: int) -> str:
    for cap, label in _SIZE_BUCKETS:
        if byte_count <= cap:
            return label
    return "very_large"


def _size_hint_for_row(row: dict) -> dict:
    """Resolve the per-row metadata bundle the catalog response surfaces.

    Renamed from `_materialized_size_hint` (which always also handled
    `local` rows; the old name was misleading). Returns a dict with up
    to four keys: `rough_size_hint`, `rows`, `size_bytes`, `partition_by`,
    `clustered_by`. Missing keys are reported as `null` in the response.

    Branches:
      - `local` / `materialized` → existing on-disk parquet stat (cheap).
      - `remote` → dispatch to the per-source-type provider; cache the
        TableMetadata for 15 min.
    """
    table_id = row["id"]
    source_type = row.get("source_type") or ""
    query_mode = row.get("query_mode") or "local"

    if query_mode in ("local", "materialized"):
        return {"rough_size_hint": _materialized_parquet_size_bucket(
            table_id, source_type, query_mode,
        )}

    if query_mode != "remote":
        return {"rough_size_hint": None}

    # Cache lookup (per-row TableMetadata).
    cached = _metadata_cache.get(table_id)
    if cached is None:
        cached = _resolve_remote_metadata(row)
        if cached is not None:
            _metadata_cache.set(table_id, cached)

    if cached is None:
        return {"rough_size_hint": None}

    return {
        "rough_size_hint": _bucket_size(cached.size_bytes) if cached.size_bytes else None,
        "rows": cached.rows,
        "size_bytes": cached.size_bytes,
        "partition_by": cached.partition_by,
        "clustered_by": cached.clustered_by,
    }


def _materialized_parquet_size_bucket(
    table_id: str, source_type: str, query_mode: str,
) -> str | None:
    """Size hint for rows whose data is on the server filesystem
    (the old `_materialized_size_hint` body). Renamed for clarity now
    that the new dispatcher is the entry point.

    Layout matches the v2 extract.duckdb contract:
      ${DATA_DIR}/extracts/<source_type>/data/<table_id>.parquet
    """
    if not source_type:
        return None
    try:
        path = (
            Path(_get_data_dir()) / "extracts" / source_type / "data"
            / f"{table_id}.parquet"
        )
        if not path.exists():
            return None
        return _bucket_size(path.stat().st_size)
    except Exception:
        # Filesystem stat() race / permissions / weird DATA_DIR — fall back
        # to null rather than crash the whole catalog response.
        return None


def _resolve_remote_metadata(row: dict) -> "TableMetadata | None":
    """Provider dispatch for a remote row. Returns None on any failure."""
    source_type = row.get("source_type") or ""
    provider = _metadata_provider_for(source_type)
    if provider is None:
        return None
    req = _build_metadata_request(row)
    if req is None:
        return None
    try:
        return provider(req)
    except Exception:
        # Defense in depth — providers are documented as never-raises,
        # but a regression would otherwise 500 the whole catalog.
        return None


def invalidate_for_table(table_id: str) -> None:
    """Drop every per-table cache so the next /api/v2/* request reflects
    the just-registered / updated / unregistered row immediately. Owned
    by the catalog module so admin.py doesn't need to know which caches
    exist.

    Imports v2_schema and v2_sample lazily — keeps catalog tests from
    pulling in BQ-extension imports they don't need.
    """
    import asyncio
    from app.api import v2_schema, v2_sample

    _table_rows_cache.clear()
    _metadata_cache.invalidate(table_id)
    v2_schema._schema_cache.invalidate(table_id)
    # Sample cache key is `f"{table_id}|{n}"`; clearing the whole sample
    # cache is heavier than precise invalidation, but registry-change
    # frequency (handful per day on a typical instance) doesn't justify
    # adding a prefix-invalidation primitive to TTLCache.
    v2_sample._sample_cache.clear()

    # Schedule a single-row re-warm so admins editing a registry row
    # see fresh data within a couple of seconds rather than waiting for
    # the next analyst to trigger a miss. Fire-and-forget; failures
    # log + skip inside the coroutine.
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        # Running inside an async context (production FastAPI path).
        asyncio.create_task(_rewarm_one_row(table_id))
    # No running event loop (e.g. called from a sync test or a sync
    # handler thread). Skip re-warm — the next live request will
    # populate via miss.


async def _rewarm_one_row(table_id: str) -> None:
    """Background single-row re-warm. Imports cache_warmup lazily to
    avoid a circular import at module load (cache_warmup.py is created
    in Task 10; until then, this function logs a warning and returns)."""
    try:
        from app.api.cache_warmup import warm_one_table
        await warm_one_table(table_id)
    except Exception:
        import logging
        logging.getLogger(__name__).warning(
            "single-row re-warm failed for %s — next live request will populate",
            table_id,
        )


def build_catalog(conn: duckdb.DuckDBPyConnection, user: dict) -> dict:
    rows = _table_rows_cache.get(_TABLE_ROWS_KEY)
    if rows is None:
        repo = TableRegistryRepository(conn)
        rows = repo.list_all()
        _table_rows_cache.set(_TABLE_ROWS_KEY, rows)

    # RBAC is enforced fresh per request. Revoking a user's access to a
    # table takes effect on their next call to this endpoint, not after the
    # cache TTL expires.
    visible = []
    for r in rows:
        if not can_access_table(user, r["id"], conn):
            continue
        hint = _size_hint_for_row(r)
        visible.append({
            "id": r["id"],
            "name": r.get("name") or r["id"],
            "description": r.get("description") or "",
            "source_type": r.get("source_type") or "",
            "query_mode": r.get("query_mode") or "local",
            "sql_flavor": _flavor_for(r.get("source_type") or ""),
            "where_examples": _examples_for(r.get("source_type") or ""),
            "fetch_via": _fetch_hint(r["id"], r.get("source_type") or ""),
            "rough_size_hint": hint.get("rough_size_hint"),
            "rows": hint.get("rows"),
            "size_bytes": hint.get("size_bytes"),
            "partition_by": hint.get("partition_by"),
            "clustered_by": hint.get("clustered_by"),
        })

    return {
        "tables": visible,
        "server_time": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/catalog")
def catalog(
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    # Plain ``def`` so FastAPI auto-offloads to the anyio thread pool —
    # build_catalog now calls `_size_hint_for_row` for every visible row,
    # which does sync `Path.stat()` / `Path.exists()` on the data volume
    # (local/materialized) or provider dispatch (remote). On local FS
    # that's microseconds, but on a network-mounted DATA_DIR (NFS / CIFS /
    # GCS-FUSE) those calls can block. Plain ``def`` means each request
    # runs on its own thread; the event loop stays free for non-catalog
    # traffic. Mirrors the Tier 1 conversion of /api/query, /api/v2/scan,
    # /api/v2/sample, /api/v2/schema — Devin Review on PR #188.
    return build_catalog(conn, user)
