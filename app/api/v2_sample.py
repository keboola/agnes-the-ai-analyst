"""GET /api/v2/sample/{table_id}?n=5 — sample rows (spec §3.3)."""

from __future__ import annotations
import logging
import math
import time
from fastapi import APIRouter, Depends, HTTPException, Query
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

_sample_cache = TTLCache(maxsize=512, ttl_seconds=3600)
_MAX_N = 100


def _sanitize_for_json(obj):
    """Recursively replace NaN / ±inf floats with None so the response
    survives JSON serialization. FastAPI's default encoder rejects these
    (``ValueError: Out of range float values are not JSON compliant``)
    even though Python's stdlib ``json`` accepts them by default. NaNs
    show up routinely in DuckDB / BigQuery scans (NULL → NaN through the
    pandas DataFrame round-trip), so the endpoint must sanitize at the
    data-prep boundary rather than rely on the serializer."""
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, list):
        return [_sanitize_for_json(x) for x in obj]
    if isinstance(obj, tuple):
        return tuple(_sanitize_for_json(x) for x in obj)
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    return obj


def _fetch_bq_sample(bq, dataset: str, table: str, n: int) -> list[dict]:
    """Fetch up to `n` sample rows from a BQ table via the DuckDB BQ extension.

    `bq.duckdb_session()` provides a DuckDB conn with the bigquery extension
    loaded + auth secret installed. SQL here is server-constructed (validated
    identifiers + LIMIT n) — a BQ BadRequest means registry corruption, not
    user fault, so it surfaces as `bq_upstream_error` (HTTP 502).
    """
    from connectors.bigquery.access import translate_bq_error
    from src.identifier_validation import validate_quoted_identifier

    # Surface "BQ not configured" as the structured 500 BqAccessError(not_configured)
    # with hint pointing at instance.yaml, NOT as the misleading 400 unsafe_identifier
    # the empty-string sentinel BqAccess would otherwise trigger from
    # validate_quoted_identifier below. Devin BUG_0002 on PR #138.
    if not bq.projects.data:
        bq.client()  # raises BqAccessError(not_configured); endpoint catches it

    # Defense in depth: registry already validates these, but the v2 API
    # endpoints are downstream of admin REST writes that might bypass that
    # gate. A `source_table` containing a backtick would otherwise break
    # out of the `…` quoted identifier and execute arbitrary BQ SQL.
    if not (validate_quoted_identifier(bq.projects.data, "BQ project")
            and validate_quoted_identifier(dataset, "BQ dataset")
            and validate_quoted_identifier(table, "BQ source_table")):
        raise ValueError("unsafe BQ identifier in registry — refusing to query")

    bq_sql = f"SELECT * FROM `{bq.projects.data}.{dataset}.{table}` LIMIT {int(n)}"
    with bq.duckdb_session() as conn:
        try:
            df = conn.execute(
                "SELECT * FROM bigquery_query(?, ?)",
                [bq.projects.billing, bq_sql],
            ).fetchdf()
            return df.to_dict(orient="records")
        except Exception as e:
            raise translate_bq_error(e, bq.projects, bad_request_status="upstream_error")


def build_sample(
    conn: duckdb.DuckDBPyConnection,
    user: dict,
    table_id: str,
    *,
    n: int,
    bq: BqAccess,
) -> dict:
    n = max(1, min(int(n), _MAX_N))

    # RBAC + existence check MUST run before cache lookup — otherwise an
    # unauthorized user can read cached sample rows fetched by an authorized one.
    repo = table_registry_repo()
    row = repo.get(table_id)
    if not row:
        raise FileNotFoundError(table_id)

    if not can_access_table(user, table_id, conn):
        raise PermissionError(table_id)

    source_type = row.get("source_type") or ""

    # Internal source — never cache. Sample rows here are RBAC-scoped per
    # caller (alice sees alice's rows; admin sees all), so a shared cache
    # would leak alice's rows to bob on the next request. The source data
    # is small + the per-request query is cheap, so skipping the cache
    # entirely is the right trade-off.
    if source_type == "internal":
        from connectors.internal.access import (
            INTERNAL_TABLES_BY_ID, build_filter_clause,
        )
        from app.auth.access import is_user_admin as _is_admin
        if table_id not in INTERNAL_TABLES_BY_ID:
            raise FileNotFoundError(table_id)
        internal_def = INTERNAL_TABLES_BY_ID[table_id]
        is_admin = _is_admin(user.get("id")) if user.get("id") else False
        where_clause = build_filter_clause(internal_def, user, is_admin)
        # Internal sources (agnes_sessions / agnes_usage / agnes_audit)
        # live in Postgres now — query through the shared engine. The
        # RBAC WHERE clause is built with already-validated user values
        # (regex-checked in connectors.internal.access._filter_value)
        # so direct interpolation is safe.
        import sqlalchemy as sa
        from src.db_pg import get_engine
        sql = f"SELECT * FROM {internal_def.source_table} {where_clause} LIMIT {int(n)}"
        with get_engine().connect() as eng_conn:
            result = eng_conn.execute(sa.text(sql))
            cols = list(result.keys())
            rows = [dict(zip(cols, row)) for row in result.fetchall()]
        return {"table_id": table_id, "rows": _sanitize_for_json(rows), "source": source_type}

    cache_key = f"{table_id}|{n}"
    cached = _sample_cache.get(cache_key)
    if cached is not None:
        return cached

    if source_type == "bigquery" and (row.get("query_mode") or "") != "materialized":
        rows = _fetch_bq_sample(bq, row.get("bucket") or "", row.get("source_table") or table_id, n)
    else:
        from app.utils import get_data_dir
        parquet = get_data_dir() / "extracts" / source_type / "data" / f"{table_id}.parquet"
        c = duckdb.connect(":memory:")
        try:
            df = c.execute(
                f"SELECT * FROM read_parquet(?) LIMIT {n}",
                [str(parquet)],
            ).fetchdf()
            rows = df.to_dict(orient="records")
        finally:
            c.close()

    rows = _sanitize_for_json(rows)
    payload = {"table_id": table_id, "rows": rows, "source": source_type}
    _sample_cache.set(cache_key, payload)
    return payload


@router.get("/sample/{table_id}")
def sample(
    table_id: str,
    n: int = Query(default=5, ge=1, le=_MAX_N),
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
    bq: BqAccess = Depends(get_bq_access),
):
    # Plain ``def`` — opens a `bq.duckdb_session()` and runs sync queries
    # through the BQ extension. See PR #188 Tier 1 entry.
    t0 = time.monotonic()
    resource = f"table:{table_id}"[:256]
    try:
        result = build_sample(conn, user, table_id, n=n, bq=bq)
        try:
            audit_repo().log(
                user_id=user.get("id"),
                action="catalog.sample",
                resource=resource,
                params={
                    "rows_returned": len(result.get("rows", [])),
                    "duration_ms": int((time.monotonic() - t0) * 1000),
                },
                result="success",
                client_kind=client_kind_from_user(user),
            )
        except Exception:
            logger.exception("audit_log write failed for catalog.sample; continuing")
        return result
    except (FileNotFoundError, PermissionError, ValueError, BqAccessError) as exc:
        try:
            if isinstance(exc, FileNotFoundError):
                status_code = 404
            elif isinstance(exc, PermissionError):
                status_code = 403
            elif isinstance(exc, ValueError):
                status_code = 400
            else:
                status_code = BqAccessError.HTTP_STATUS.get(exc.kind, 500)  # type: ignore[union-attr]
            audit_repo().log(
                user_id=user.get("id"),
                action="catalog.sample",
                resource=resource,
                params={"duration_ms": int((time.monotonic() - t0) * 1000),
                        "error": str(exc)[:200]},
                result=f"error.{status_code}",
                client_kind=client_kind_from_user(user),
            )
        except Exception:
            logger.exception("audit_log write failed on error path for catalog.sample; continuing")
        if isinstance(exc, FileNotFoundError):
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
