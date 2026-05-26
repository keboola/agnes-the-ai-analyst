"""GET /api/v2/schema/{table_id} — table column metadata (spec §3.2)."""

from __future__ import annotations
import logging
import time
from fastapi import APIRouter, Depends, HTTPException
import duckdb

from app.auth.dependencies import get_current_user, _get_db
from src.audit_helpers import client_kind_from_user
from src.rbac import can_access_table
from app.api.v2_cache import TTLCache
from connectors.bigquery.access import BqAccess, BqAccessError, get_bq_access

from src.repositories import (
    audit_repo,
    table_registry_repo,
)
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
    """Fetch column list via the shared ``_fetch_bq_columns_full_impl`` helper.

    Pre-#155 this had its own INFORMATION_SCHEMA.COLUMNS query; consolidating
    with ``_fetch_bq_table_options`` (now also delegating to the same shared
    SQL) halves the BQ job count on cache miss. Returns the schema-endpoint
    column shape: name / type / nullable / description.

    Calls the raising variant so BQ exceptions reach ``translate_bq_error``
    with their original type (Forbidden → 502, BadRequest → 400, etc.).
    """
    from connectors.bigquery.access import _fetch_bq_columns_full_impl, translate_bq_error, BqAccessError

    try:
        rows = _fetch_bq_columns_full_impl(bq, dataset, table)
    except (ValueError, BqAccessError):
        # ValueError ("unsafe identifier") and BqAccessError propagate
        # unchanged — the endpoint's existing handlers expect those types.
        raise
    except Exception as e:
        # Any other BQ-side exception goes through translate_bq_error so
        # the response status is classified correctly.
        raise translate_bq_error(e, bq.projects, bad_request_status="upstream_error")

    return [
        {
            "name": r["name"],
            "type": r["type"],
            "nullable": r["nullable"],
            "description": "",
        }
        for r in rows
    ]


def _fetch_bq_table_options(bq, dataset: str, table: str) -> dict:
    """Best-effort fetch of partition/cluster info via the shared
    `fetch_bq_columns_full` helper.

    Returns ``{}`` on ANY failure (best-effort). Same load-bearing
    contract as before: the /schema endpoint must keep returning 200
    with empty partition info when this fails.
    """
    from connectors.bigquery.access import fetch_bq_columns_full

    rows = fetch_bq_columns_full(bq, dataset, table)
    if not rows:
        return {}

    partition_by = next(
        (r["name"] for r in rows if r["is_partitioning_column"]),
        None,
    )
    clustered_rows = [r for r in rows if r["clustering_ordinal_position"] is not None]
    clustered_rows.sort(key=lambda r: r["clustering_ordinal_position"])
    clustered_by = [r["name"] for r in clustered_rows]
    return {"partition_by": partition_by, "clustered_by": clustered_by}


def build_schema(
    conn: duckdb.DuckDBPyConnection,
    user: dict,
    table_id: str,
    *,
    bq: BqAccess,
) -> dict:
    # RBAC + existence check MUST run before cache lookup — otherwise an
    # unauthorized user can read cached schema fetched by an authorized one.
    repo = table_registry_repo()
    row = repo.get(table_id)
    if not row:
        raise NotFound(table_id)

    if not can_access_table(user, table_id, conn):
        raise PermissionError(table_id)

    cached = _schema_cache.get(table_id)
    if cached is not None:
        return cached

    return build_schema_uncached(conn, table_id, bq=bq, row=row)


def build_schema_uncached(
    conn: duckdb.DuckDBPyConnection,
    table_id: str,
    *,
    bq: BqAccess,
    row: dict | None = None,
) -> dict:
    """Build the schema response and populate `_schema_cache`. **Skips
    RBAC and cache-hit short-circuit** — call only from contexts where
    those are unnecessary (warmup) or already enforced upstream
    (`build_schema`).

    Pass `row` from the upstream caller's `repo.get(table_id)` to avoid
    a redundant DB round-trip; if not provided, `build_schema_uncached`
    fetches it itself (the warmup-direct call site).
    """
    if row is None:
        repo = table_registry_repo()
        row = repo.get(table_id)
        if not row:
            raise NotFound(table_id)

    source_type = row.get("source_type") or ""
    query_mode = row.get("query_mode") or ""
    if source_type == "internal":
        # Internal data sources live in Postgres post-cutover; the
        # connector introspector takes a ``system_db_path`` for
        # signature back-compat and ignores it.
        from connectors.internal.access import get_schema as _get_internal_schema
        cols = _get_internal_schema("", table_id)
        payload = {
            "table_id": table_id,
            "source_type": source_type,
            "sql_flavor": "postgres",
            "columns": [
                {"name": c["name"], "type": c["type"], "nullable": c["nullable"], "description": ""}
                for c in cols
            ],
            "partition_by": None,
            "clustered_by": [],
            "where_dialect_hints": {},
        }
    # Issue #261: a `source_type='bigquery'` row with `query_mode='materialized'`
    # has the data on local disk as a parquet — same shape as Keboola local
    # tables. Hitting BigQuery INFORMATION_SCHEMA on every schema call was
    # the root cause of the materialized-schema cold-start anomaly observed
    # in the 0.51.0 perf tests (4.6 s vs 1.0 s for remote VIEW). Use the
    # local-parquet branch for any materialized source regardless of
    # `source_type` — the parquet is the source of truth.
    elif source_type == "bigquery" and query_mode != "materialized":
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

    _schema_cache.set(table_id, payload)
    return payload


@router.get("/schema/{table_id}")
def schema(
    table_id: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
    bq: BqAccess = Depends(get_bq_access),
):
    # Plain ``def`` — opens a `bq.duckdb_session()` and runs sync metadata
    # queries through the BQ extension. See PR #188 Tier 1 entry.
    t0 = time.monotonic()
    resource = f"table:{table_id}"[:256]
    try:
        result = build_schema(conn, user, table_id, bq=bq)
        try:
            audit_repo().log(
                user_id=user.get("id"),
                action="catalog.schema",
                resource=resource,
                params={"duration_ms": int((time.monotonic() - t0) * 1000)},
                result="success",
                client_kind=client_kind_from_user(user),
            )
        except Exception:
            logger.exception("audit_log write failed for catalog.schema; continuing")
        return result
    except (NotFound, PermissionError, ValueError, BqAccessError) as exc:
        try:
            if isinstance(exc, NotFound):
                status_code = 404
            elif isinstance(exc, PermissionError):
                status_code = 403
            elif isinstance(exc, ValueError):
                status_code = 400
            else:
                status_code = BqAccessError.HTTP_STATUS.get(exc.kind, 500)  # type: ignore[union-attr]
            audit_repo().log(
                user_id=user.get("id"),
                action="catalog.schema",
                resource=resource,
                params={"duration_ms": int((time.monotonic() - t0) * 1000),
                        "error": str(exc)[:200]},
                result=f"error.{status_code}",
                client_kind=client_kind_from_user(user),
            )
        except Exception:
            logger.exception("audit_log write failed on error path for catalog.schema; continuing")
        if isinstance(exc, NotFound):
            raise HTTPException(status_code=404, detail=f"table {table_id!r} not found")
        if isinstance(exc, PermissionError):
            from src.rbac import table_not_in_stack_message
            raise HTTPException(
                status_code=403,
                detail=table_not_in_stack_message(table_id),
            )
        if isinstance(exc, ValueError):
            raise HTTPException(
                status_code=400,
                detail={"error": "unsafe_identifier", "message": str(exc), "details": {}},
            )
        raise HTTPException(
            status_code=BqAccessError.HTTP_STATUS.get(exc.kind, 500),  # type: ignore[union-attr]
            detail={"error": exc.kind, "message": exc.message, "details": exc.details},  # type: ignore[union-attr]
        )
