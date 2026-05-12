"""GET /api/v2/catalog — list tables visible to caller (spec §3.1).

History note
------------
0.47.0 enriched remote rows with BigQuery metadata (rows / size_bytes /
partition_by / clustered_by) by fetching from BQ *inside the request*
through a per-table TTL cache. On a cold cache that fanned out to O(N)
sequential BQ jobs API roundtrips and reliably exceeded the CLI's 30 s
``httpx.ReadTimeout`` against partitioned tables. This module now reads
those fields exclusively from the persistent ``bq_metadata_cache`` table
(populated by ``app/api/bq_metadata_refresh.py`` on a scheduler tick).
The request path never calls BQ.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb
from fastapi import APIRouter, Depends

from app.api.v2_cache import TTLCache
from app.auth.dependencies import _get_db, get_current_user
from app.utils import get_data_dir as _get_data_dir
from src.rbac import can_access_table
from src.repositories.bq_metadata_cache import BqMetadataCacheRepository
from src.repositories.table_registry import TableRegistryRepository
from src.repositories.audit import AuditRepository

logger = logging.getLogger(__name__)
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


def _flavor_for(source_type: str) -> str:
    return "bigquery" if source_type == "bigquery" else "duckdb"


# Generic ``where_examples`` templates the catalog surfaces as a starting
# point for AI consumers. Each entry is a tuple of ``(predicate_text,
# required_columns)``: the template is only included in the response when
# every required column is present in the table's actual schema (from
# ``bq_metadata_cache.known_columns``). This prevents the old behavior of
# always advertising ``country_code = 'CZ'`` on tables that have no
# ``country_code`` column at all.
_BQ_WHERE_TEMPLATES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("event_date > DATE '2026-01-01'", ("event_date",)),
    ("country_code = 'CZ' AND platform = 'web'", ("country_code", "platform")),
)


def _examples_for(source_type: str, known_columns: list[str] | None) -> list[str]:
    """Return generic ``where_examples`` filtered against the table's
    actual columns. ``known_columns`` comes from the persistent metadata
    cache; when it is unknown (None) or empty, return an empty list
    instead of a possibly-wrong template — silence is better than
    misleading hints for AI consumers."""
    if source_type != "bigquery":
        return []
    if not known_columns:
        return []
    cols = set(known_columns)
    return [
        predicate
        for predicate, required in _BQ_WHERE_TEMPLATES
        if all(c in cols for c in required)
    ]


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


def _materialized_parquet_size_bucket(
    table_id: str, source_type: str, query_mode: str,
) -> str | None:
    """Size hint for rows whose data is on the server filesystem
    (``local`` or ``materialized``). Cheap ``Path.stat()``; never blocks.

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


def _hint_for_row(
    row: dict[str, Any],
    bq_cache_index: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Resolve the per-row metadata bundle the catalog response surfaces.

    Branches:
      - ``local`` / ``materialized`` → on-disk parquet ``stat()`` (cheap).
      - ``remote`` (BigQuery) → pre-computed row from ``bq_metadata_cache``,
        populated by the scheduler-driven refresh. Never touches BQ here.

    Always returns ``metadata_freshness`` (``fresh`` / ``stale`` /
    ``never_fetched`` / ``error`` / ``not_applicable``) so AI consumers can
    decide whether to trust ``rows`` / ``size_bytes`` or treat them as
    advisory.
    """
    table_id = row["id"]
    source_type = row.get("source_type") or ""
    query_mode = row.get("query_mode") or "local"

    if query_mode in ("local", "materialized"):
        return {
            "rough_size_hint": _materialized_parquet_size_bucket(
                table_id, source_type, query_mode,
            ),
            "entity_type": None,
            "known_columns": [],
            "metadata_freshness": "not_applicable",
        }

    if query_mode != "remote":
        return {
            "rough_size_hint": None,
            "entity_type": None,
            "known_columns": [],
            "metadata_freshness": "not_applicable",
        }

    # Remote: read from the persistent cache; never call BQ here.
    from app.api.bq_metadata_refresh import compute_freshness
    cache_row = bq_cache_index.get(table_id)
    freshness = compute_freshness(cache_row)

    if cache_row is None:
        return {
            "rough_size_hint": None,
            "rows": None,
            "size_bytes": None,
            "partition_by": None,
            "clustered_by": [],
            "entity_type": None,
            "known_columns": [],
            "metadata_freshness": freshness,
        }

    size_bytes = cache_row.get("size_bytes")
    return {
        "rough_size_hint": _bucket_size(size_bytes) if size_bytes is not None else None,
        "rows": cache_row.get("rows"),
        "size_bytes": size_bytes,
        "partition_by": cache_row.get("partition_by"),
        "clustered_by": cache_row.get("clustered_by") or [],
        "entity_type": cache_row.get("entity_type"),
        "known_columns": cache_row.get("known_columns") or [],
        "metadata_freshness": freshness,
    }


def invalidate_for_table(table_id: str) -> None:
    """Drop every per-table cache so the next /api/v2/* request reflects
    the just-registered / updated / unregistered row immediately. Owned
    by the catalog module so admin.py doesn't need to know which caches
    exist.

    The persistent ``bq_metadata_cache`` row is NOT invalidated here —
    the scheduler-driven refresh owns that lifecycle. Admins who need
    an immediate refresh after a registry edit should hit
    ``POST /api/v2/metadata-cache/refresh?table=<id>``.
    """
    from app.api import v2_sample, v2_schema

    _table_rows_cache.clear()
    v2_schema._schema_cache.invalidate(table_id)
    # Sample cache key is `f"{table_id}|{n}"`; clearing the whole sample
    # cache is heavier than precise invalidation, but registry-change
    # frequency (handful per day on a typical instance) doesn't justify
    # adding a prefix-invalidation primitive to TTLCache.
    v2_sample._sample_cache.clear()


def build_catalog(conn: duckdb.DuckDBPyConnection, user: dict) -> dict:
    rows = _table_rows_cache.get(_TABLE_ROWS_KEY)
    if rows is None:
        repo = TableRegistryRepository(conn)
        rows = repo.list_all()
        _table_rows_cache.set(_TABLE_ROWS_KEY, rows)

    # One DB read for all remote-row metadata. Indexed by table_id so the
    # per-row loop below stays O(N).
    bq_cache_index: dict[str, dict[str, Any]] = {
        r["table_id"]: r for r in BqMetadataCacheRepository(conn).list_all()
    }

    # RBAC is enforced fresh per request. Revoking a user's access to a
    # table takes effect on their next call to this endpoint, not after the
    # cache TTL expires.
    visible = []
    for r in rows:
        if not can_access_table(user, r["id"], conn):
            continue
        hint = _hint_for_row(r, bq_cache_index)
        visible.append({
            "id": r["id"],
            "name": r.get("name") or r["id"],
            "description": r.get("description") or "",
            "source_type": r.get("source_type") or "",
            "query_mode": r.get("query_mode") or "local",
            "sql_flavor": _flavor_for(r.get("source_type") or ""),
            "where_examples": _examples_for(
                r.get("source_type") or "", hint.get("known_columns"),
            ),
            "fetch_via": _fetch_hint(r["id"], r.get("source_type") or ""),
            "rough_size_hint": hint.get("rough_size_hint"),
            "rows": hint.get("rows"),
            "size_bytes": hint.get("size_bytes"),
            "partition_by": hint.get("partition_by"),
            "clustered_by": hint.get("clustered_by") or [],
            "entity_type": hint.get("entity_type"),
            "metadata_freshness": hint.get("metadata_freshness"),
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
    # the request path is pure local I/O (DuckDB reads + filesystem
    # stat()) and uses a sync DuckDB cursor.
    t0 = time.monotonic()
    try:
        result = build_catalog(conn, user)
        try:
            AuditRepository(conn).log(
                user_id=user.get("id"),
                action="catalog.list",
                resource="catalog",
                params={
                    "rows_returned": len(result.get("tables", [])),
                    "duration_ms": int((time.monotonic() - t0) * 1000),
                },
                result="success",
                client_kind="cli",  # catalog is primarily CLI-driven (agnes catalog)
            )
        except Exception:
            logger.exception("audit_log write failed for catalog.list; continuing")
        return result
    except Exception as exc:
        try:
            AuditRepository(conn).log(
                user_id=user.get("id"),
                action="catalog.list",
                resource="catalog",
                params={"error": str(exc)[:200], "duration_ms": int((time.monotonic() - t0) * 1000)},
                result=f"error.{type(exc).__name__}",
                client_kind="cli",
            )
        except Exception:
            logger.exception("audit_log write failed on error path for catalog.list; continuing")
        raise
