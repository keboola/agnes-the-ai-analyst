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

_catalog_cache = TTLCache(maxsize=1024, ttl_seconds=300)  # per-user, 5 min


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
    cache_key = f"{user.get('email', '?')}|catalog"
    cached = _catalog_cache.get(cache_key)
    if cached is not None:
        return cached

    repo = TableRegistryRepository(conn)
    rows = repo.list_all()

    visible = []
    for r in rows:
        if user.get("role") != "admin" and not can_access_table(user, r["id"], conn):
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

    payload = {
        "tables": visible,
        "server_time": datetime.now(timezone.utc).isoformat(),
    }
    _catalog_cache.set(cache_key, payload)
    return payload


@router.get("/catalog")
async def catalog(
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    return build_catalog(conn, user)
