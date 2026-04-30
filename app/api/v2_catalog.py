"""GET /api/v2/catalog — list tables visible to caller (spec §3.1)."""

from __future__ import annotations
from datetime import datetime, timezone
from fastapi import APIRouter, Depends
import duckdb

from app.auth.dependencies import get_current_user, _get_db
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
        return f"da fetch {table_id} --select <cols> --where '<BQ predicate>' --limit <N>"
    return "already local — query directly via `da query`"


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
            "rough_size_hint": None,  # populated by Task 8 schema endpoint when called
        })

    return {
        "tables": visible,
        "server_time": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/catalog")
async def catalog(
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    return build_catalog(conn, user)
