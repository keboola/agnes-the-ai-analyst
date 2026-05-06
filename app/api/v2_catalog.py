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


def _materialized_size_hint(table_id: str, source_type: str, query_mode: str) -> str | None:
    """Return a rough size bucket for a row whose data is on the server's
    local filesystem (any `query_mode` that produces a parquet — `local` and
    `materialized`). Returns ``None`` for `remote` (size requires a BQ
    INFORMATION_SCHEMA round-trip; tracked separately) and for tables whose
    parquet hasn't been materialised yet so the AI gets ``null`` not a
    misleading "small".

    Layout matches the v2 extract.duckdb contract:
      ${DATA_DIR}/extracts/<source_type>/data/<table_id>.parquet
    """
    if query_mode == "remote":
        return None
    if not source_type:
        return None
    try:
        path = Path(_get_data_dir()) / "extracts" / source_type / "data" / f"{table_id}.parquet"
        if not path.exists():
            return None
        return _bucket_size(path.stat().st_size)
    except Exception:
        # Filesystem stat() race / permissions / weird DATA_DIR — fall back
        # to null rather than crash the whole catalog response.
        return None


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
        visible.append({
            "id": r["id"],
            "name": r.get("name") or r["id"],
            "description": r.get("description") or "",
            "source_type": r.get("source_type") or "",
            "query_mode": r.get("query_mode") or "local",
            "sql_flavor": _flavor_for(r.get("source_type") or ""),
            "where_examples": _examples_for(r.get("source_type") or ""),
            "fetch_via": _fetch_hint(r["id"], r.get("source_type") or ""),
            "rough_size_hint": _materialized_size_hint(
                r["id"], r.get("source_type") or "",
                r.get("query_mode") or "local",
            ),
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
    # build_catalog now calls `_materialized_size_hint` for every visible
    # row, which does sync `Path.stat()` / `Path.exists()` on the data
    # volume. On local FS that's microseconds, but on a network-mounted
    # DATA_DIR (NFS / CIFS / GCS-FUSE) those calls can block. Plain ``def``
    # means each request runs on its own thread; the event loop stays
    # free for non-catalog traffic. Mirrors the Tier 1 conversion of
    # /api/query, /api/v2/scan, /api/v2/sample, /api/v2/schema —
    # Devin Review on PR #188.
    return build_catalog(conn, user)
