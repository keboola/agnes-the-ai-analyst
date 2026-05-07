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
    """Fetch column list via the shared `fetch_bq_columns_full` helper.

    Pre-#155 this had its own INFORMATION_SCHEMA.COLUMNS query; consolidating
    with `_fetch_bq_table_options` (now also delegating to the same helper)
    halves the BQ job count on cache miss. Returns the schema-endpoint
    column shape: name / type / nullable / description.
    """
    from connectors.bigquery.access import fetch_bq_columns_full, translate_bq_error, BqAccessError
    from src.identifier_validation import validate_quoted_identifier

    # Surface "BQ not configured" as the structured 500 BqAccessError
    # (with hint), not a misleading empty-list. Mirrors pre-refactor
    # behavior — see Devin BUG_0002 in the original docstring.
    if not bq.projects.data:
        bq.client()  # raises BqAccessError(not_configured); endpoint catches it

    # Defense in depth — refuse unsafe identifiers before any SQL construction.
    # The helper also validates, but we need to surface this as ValueError (not
    # None) here so the endpoint returns 400 unsafe_identifier.
    if not (validate_quoted_identifier(bq.projects.data, "BQ project")
            and validate_quoted_identifier(dataset, "BQ dataset")
            and validate_quoted_identifier(table, "BQ source_table")):
        raise ValueError("unsafe BQ identifier in registry — refusing to query")

    # Run the shared single-round-trip query. Capture the original BQ exception
    # so translate_bq_error can classify it correctly (Forbidden → 502, etc.)
    # rather than wrapping a generic RuntimeError that translate_bq_error would
    # re-raise unclassified.
    _last_exc: list = []

    def _session_factory_capturing(projects):
        """Thin wrapper around bq.duckdb_session() that intercepts exceptions
        and stashes them in _last_exc before re-raising, so the caller's
        translate_bq_error path sees the original Google API exception type."""
        from contextlib import contextmanager

        @contextmanager
        def _cm():
            with bq.duckdb_session() as conn:
                try:
                    yield conn
                except Exception as exc:
                    _last_exc.append(exc)
                    raise
        return _cm()

    # Build a thin BqAccess wrapper that uses our capturing session factory.
    from connectors.bigquery.access import BqAccess
    bq_capturing = BqAccess(
        bq.projects,
        client_factory=lambda p: bq.client(),
        duckdb_session_factory=_session_factory_capturing,
    )

    rows = fetch_bq_columns_full(bq_capturing, dataset, table)
    if rows is None:
        # fetch_bq_columns_full swallowed the exception. Re-raise via
        # translate_bq_error using the captured original exception if available,
        # so Forbidden/BadRequest classifications survive the helper boundary.
        orig = _last_exc[0] if _last_exc else RuntimeError(
            "BQ INFORMATION_SCHEMA.COLUMNS query failed"
        )
        raise translate_bq_error(orig, bq.projects, bad_request_status="upstream_error")

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
    repo = TableRegistryRepository(conn)
    row = repo.get(table_id)
    if not row:
        raise NotFound(table_id)

    if not can_access_table(user, table_id, conn):
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
def schema(
    table_id: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
    bq: BqAccess = Depends(get_bq_access),
):
    # Plain ``def`` — opens a `bq.duckdb_session()` and runs sync metadata
    # queries through the BQ extension. See PR #188 Tier 1 entry.
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
