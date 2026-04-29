"""GET /api/v2/schema/{table_id} — table column metadata (spec §3.2)."""

from __future__ import annotations
import logging
from fastapi import APIRouter, Depends, HTTPException
import duckdb

from app.auth.dependencies import get_current_user, _get_db
from src.rbac import can_access_table
from src.repositories.table_registry import TableRegistryRepository
from app.api.v2_cache import TTLCache
from connectors.bigquery.access import BqAccess, BqAccessError, get_bq_access

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


def _fetch_bq_schema(bq, dataset: str, table: str) -> list[dict]:
    """Fetch column list via INFORMATION_SCHEMA.COLUMNS using DuckDB BQ extension.

    `bq.duckdb_session()` provides a DuckDB conn with the bigquery extension
    loaded + auth secret installed. SQL here is server-constructed (queries
    INFORMATION_SCHEMA.COLUMNS with validated identifiers, no user-derived
    fragments), so a BQ BadRequest means registry corruption, not user input
    → surfaces as `bq_upstream_error` (HTTP 502), same as `/sample`, opposite
    of `/scan*`.
    """
    from connectors.bigquery.access import translate_bq_error
    from src.identifier_validation import validate_quoted_identifier

    # Defense in depth (cf. v2_sample) — registry already validates these,
    # but the v2 endpoints are downstream of admin REST writes that could
    # bypass that gate. A backtick in `dataset` would otherwise break out
    # of `…` quoting and execute arbitrary BQ SQL.
    if not (validate_quoted_identifier(bq.projects.data, "BQ project")
            and validate_quoted_identifier(dataset, "BQ dataset")
            and validate_quoted_identifier(table, "BQ source_table")):
        raise ValueError("unsafe BQ identifier in registry — refusing to query")

    bq_sql = (
        f"SELECT column_name, data_type, is_nullable "
        f"FROM `{bq.projects.data}.{dataset}.INFORMATION_SCHEMA.COLUMNS` "
        f"WHERE table_name = ? ORDER BY ordinal_position"
    )
    with bq.duckdb_session() as conn:
        try:
            rows = conn.execute(
                "SELECT * FROM bigquery_query(?, ?, ?)",
                [bq.projects.billing, bq_sql, table],
            ).fetchall()
        except Exception as e:
            raise translate_bq_error(e, bq.projects, bad_request_status="upstream_error")
    return [
        {
            "name": r[0],
            "type": r[1],
            "nullable": r[2] == "YES",
            "description": "",
        }
        for r in rows
    ]


def _fetch_bq_table_options(bq, dataset: str, table: str) -> dict:
    """Best-effort fetch of partition/cluster info from INFORMATION_SCHEMA.COLUMNS.

    BigQuery exposes partition + cluster metadata as per-column flags:
      - `is_partitioning_column` ('YES' / 'NO') — at most one column per table
      - `clustering_ordinal_position` (INT64, null for non-clustered columns;
        otherwise 1, 2, ... in cluster-key order)

    Returns `{}` on ANY failure (best-effort). The outer
    `try/except Exception → return {}` is a load-bearing contract: the
    /schema endpoint must keep returning 200 with empty partition info even
    when this query fails (e.g. on permissioned tables, on cross-project
    misconfigurations). DO NOT route this through `translate_bq_error` —
    that would convert errors to BqAccessError which the endpoint would 502
    on. See tests/test_v2_schema.py::test_schema_returns_200_with_empty_…
    """
    from src.identifier_validation import validate_quoted_identifier

    if not (validate_quoted_identifier(bq.projects.data, "BQ project")
            and validate_quoted_identifier(dataset, "BQ dataset")
            and validate_quoted_identifier(table, "BQ source_table")):
        return {}  # Best-effort; refuse to query unsafe identifiers.

    try:
        with bq.duckdb_session() as conn:
            bq_sql = (
                f"SELECT column_name, is_partitioning_column, clustering_ordinal_position "
                f"FROM `{bq.projects.data}.{dataset}.INFORMATION_SCHEMA.COLUMNS` "
                f"WHERE table_name = ? "
                f"ORDER BY clustering_ordinal_position NULLS LAST"
            )
            rows = conn.execute(
                "SELECT * FROM bigquery_query(?, ?, ?)",
                [bq.projects.billing, bq_sql, table],
            ).fetchall()
        if not rows:
            return {}
        partition_by = next(
            (r[0] for r in rows if (r[1] or "").upper() == "YES"),
            None,
        )
        clustered_by = [r[0] for r in rows if r[2] is not None]
        return {"partition_by": partition_by, "clustered_by": clustered_by}
    except Exception as e:
        logger.warning(
            "BQ table options fetch failed for %s.%s.%s: %s",
            bq.projects.data, dataset, table, e,
        )
        return {}


def build_schema(
    conn: duckdb.DuckDBPyConnection,
    user: dict,
    table_id: str,
    *,
    bq: BqAccess,
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
        columns = _fetch_bq_schema(bq, dataset, source_table)
        opts = _fetch_bq_table_options(bq, dataset, source_table)
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
    bq: BqAccess = Depends(get_bq_access),
):
    try:
        return build_schema(conn, user, table_id, bq=bq)
    except NotFound:
        raise HTTPException(status_code=404, detail=f"table {table_id!r} not found")
    except PermissionError:
        raise HTTPException(status_code=403, detail="not authorized for this table")
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail={"error": "unsafe_identifier", "message": str(e), "details": {}},
        )
    except BqAccessError as e:
        raise HTTPException(
            status_code=BqAccessError.HTTP_STATUS.get(e.kind, 500),
            detail={"error": e.kind, "message": e.message, "details": e.details},
        )
