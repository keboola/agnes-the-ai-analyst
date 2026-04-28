"""GET /api/v2/schema/{table_id} — table column metadata (spec §3.2)."""

from __future__ import annotations
import logging
from fastapi import APIRouter, Depends, HTTPException
import duckdb

from app.auth.dependencies import get_current_user, _get_db
from app.instance_config import get_value
from src.rbac import can_access_table
from src.repositories.table_registry import TableRegistryRepository
from app.api.v2_cache import TTLCache

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v2", tags=["v2"])

_schema_cache = TTLCache(maxsize=512, ttl_seconds=3600)


class NotFound(Exception):
    pass


_BQ_DIALECT_HINTS = {
    "date_literal": "DATE '2026-01-01'",
    "timestamp_literal": "TIMESTAMP '2026-01-01 00:00:00 UTC'",
    "interval_subtract": "DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)",
    "regex": "REGEXP_CONTAINS(field, r'pattern')",
    "cast": "CAST(x AS INT64)",
}


def _fetch_bq_schema(project: str, dataset: str, table: str) -> list[dict]:
    """Fetch column list via INFORMATION_SCHEMA.COLUMNS using DuckDB BQ extension."""
    import duckdb
    from connectors.bigquery.auth import get_metadata_token

    token = get_metadata_token()
    conn = duckdb.connect(":memory:")
    try:
        conn.execute("INSTALL bigquery FROM community; LOAD bigquery;")
        escaped = token.replace("'", "''")
        conn.execute(f"CREATE OR REPLACE SECRET bq_s (TYPE bigquery, ACCESS_TOKEN '{escaped}')")
        bq_sql = (
            f"SELECT column_name, data_type, is_nullable "
            f"FROM `{project}.{dataset}.INFORMATION_SCHEMA.COLUMNS` "
            f"WHERE table_name = ? ORDER BY ordinal_position"
        )
        rows = conn.execute(
            "SELECT * FROM bigquery_query(?, ?, ?)",
            [project, bq_sql, table],
        ).fetchall()
        return [
            {
                "name": r[0],
                "type": r[1],
                "nullable": r[2] == "YES",
                "description": "",
            }
            for r in rows
        ]
    finally:
        conn.close()


def _fetch_bq_table_options(project: str, dataset: str, table: str) -> dict:
    """Best-effort fetch of partition/cluster info; returns empty dict on miss."""
    import duckdb
    from connectors.bigquery.auth import get_metadata_token

    try:
        token = get_metadata_token()
        conn = duckdb.connect(":memory:")
        try:
            conn.execute("INSTALL bigquery FROM community; LOAD bigquery;")
            escaped = token.replace("'", "''")
            conn.execute(f"CREATE OR REPLACE SECRET bq_s (TYPE bigquery, ACCESS_TOKEN '{escaped}')")
            bq_sql = (
                f"SELECT partition_column, cluster_columns "
                f"FROM `{project}.{dataset}.INFORMATION_SCHEMA.TABLES` "
                f"WHERE table_name = ?"
            )
            row = conn.execute(
                "SELECT * FROM bigquery_query(?, ?, ?)",
                [project, bq_sql, table],
            ).fetchone()
            if not row:
                return {}
            return {
                "partition_by": row[0],
                "clustered_by": (row[1] or "").split(",") if row[1] else [],
            }
        finally:
            conn.close()
    except Exception as e:
        logger.warning("BQ table options fetch failed for %s.%s.%s: %s", project, dataset, table, e)
        return {}


def build_schema(
    conn: duckdb.DuckDBPyConnection,
    user: dict,
    table_id: str,
    *,
    project_id: str,
) -> dict:
    # RBAC + existence check MUST run before cache lookup — otherwise an
    # unauthorized user can read cached schema fetched by an authorized one.
    repo = TableRegistryRepository(conn)
    row = repo.get(table_id)
    if not row:
        raise NotFound(table_id)

    if user.get("role") != "admin" and not can_access_table(user, table_id, conn):
        raise PermissionError(table_id)

    cache_key = f"{table_id}"
    cached = _schema_cache.get(cache_key)
    if cached is not None:
        return cached

    source_type = row.get("source_type") or ""
    if source_type == "bigquery":
        dataset = row.get("bucket") or ""
        source_table = row.get("source_table") or table_id
        columns = _fetch_bq_schema(project_id, dataset, source_table)
        opts = _fetch_bq_table_options(project_id, dataset, source_table)
        payload = {
            "table_id": table_id,
            "source_type": source_type,
            "sql_flavor": "bigquery",
            "columns": columns,
            "partition_by": opts.get("partition_by"),
            "clustered_by": opts.get("clustered_by", []),
            "where_dialect_hints": _BQ_DIALECT_HINTS,
        }
    else:
        # Local source — read schema from the parquet via DuckDB
        from pathlib import Path
        from app.utils import get_data_dir
        parquet = (
            get_data_dir() / "extracts" / source_type / "data" / f"{table_id}.parquet"
        )
        local_conn = duckdb.connect(":memory:")
        try:
            cols = local_conn.execute(
                "DESCRIBE SELECT * FROM read_parquet(?)", [str(parquet)]
            ).fetchall()
        finally:
            local_conn.close()
        payload = {
            "table_id": table_id,
            "source_type": source_type,
            "sql_flavor": "duckdb",
            "columns": [
                {"name": c[0], "type": c[1], "nullable": c[2] == "YES", "description": ""}
                for c in cols
            ],
            "partition_by": None,
            "clustered_by": [],
            "where_dialect_hints": {},
        }

    _schema_cache.set(cache_key, payload)
    return payload


@router.get("/schema/{table_id}")
async def schema(
    table_id: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    project_id = get_value("data_source", "bigquery", "project", default="") or ""
    try:
        return build_schema(conn, user, table_id, project_id=project_id)
    except NotFound:
        raise HTTPException(status_code=404, detail=f"table {table_id!r} not found")
    except PermissionError:
        raise HTTPException(status_code=403, detail="not authorized for this table")
