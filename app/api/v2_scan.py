"""POST /api/v2/scan and POST /api/v2/scan/estimate (spec §3.4 + §3.5)."""

from __future__ import annotations
import logging
import re
from typing import Optional

import pyarrow as pa
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
import duckdb

from app.auth.dependencies import get_current_user, _get_db
from app.instance_config import get_value
from src.rbac import can_access_table
from src.repositories.table_registry import TableRegistryRepository
from app.api.where_validator import (
    validate_where, safe_where_predicate, WhereValidationError,
)
from app.api.v2_schema import build_schema  # reused for column resolution
from app.api.v2_arrow import arrow_table_to_ipc_bytes, CONTENT_TYPE
from app.api.v2_quota import QuotaTracker, QuotaExceededError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v2", tags=["v2"])


class ScanRequest(BaseModel):
    table_id: str
    select: Optional[list[str]] = None
    where: Optional[str] = None
    limit: Optional[int] = Field(default=None, ge=1)
    order_by: Optional[list[str]] = None


def _resolve_schema(conn, user, table_id: str, project_id: str) -> dict:
    """Get {column: type} dict for the target table — used by validator + projection check."""
    s = build_schema(conn, user, table_id, project_id=project_id)
    return {c["name"]: c["type"] for c in s.get("columns", [])}


def _bq_dry_run_bytes(project: str, sql: str) -> int:
    """Run a BQ dry-run via the google-cloud-bigquery client and return totalBytesProcessed."""
    from google.cloud import bigquery
    from google.api_core.client_options import ClientOptions
    client = bigquery.Client(
        project=project,
        client_options=ClientOptions(quota_project_id=project),
    )
    job = client.query(
        sql,
        job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False),
    )
    return int(job.total_bytes_processed or 0)


_ORDER_BY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\s+(ASC|DESC))?$", re.IGNORECASE)


def _validate_order_by(order_by: list[str] | None, schema: dict) -> None:
    """Reject anything other than `<column>` or `<column> ASC|DESC` against the schema.
    Without this, `order_by` is concatenated raw into the FROM clause SQL — exploitable."""
    if not order_by:
        return
    known = {c.lower() for c in schema}
    for entry in order_by:
        s = (entry or "").strip()
        if not _ORDER_BY_RE.match(s):
            raise ValueError(f"invalid order_by entry: {entry!r}")
        col = s.split()[0].lower()
        if col not in known:
            raise ValueError(f"unknown order_by column: {entry!r}")


def _quote_order_by_bq(entry: str) -> str:
    """Backtick-quote the column part of an order_by entry, preserve direction."""
    parts = entry.strip().split()
    return f"`{parts[0]}`" + ("" if len(parts) == 1 else " " + " ".join(parts[1:]))


def _quote_order_by_duckdb(entry: str) -> str:
    parts = entry.strip().split()
    return f'"{parts[0]}"' + ("" if len(parts) == 1 else " " + " ".join(parts[1:]))


def _build_bq_sql(
    table_row: dict, project_id: str, req: ScanRequest, *, safe_where: str | None = None,
) -> str:
    """Build the BQ SQL string. ``safe_where`` MUST be the comment-stripped
    fragment from ``safe_where_predicate`` — splicing ``req.where`` raw lets a
    `1=1 --` predicate comment out everything that follows (LIMIT/ORDER BY).

    Identifier quoting: column names are validated against the schema before
    we get here, but reserved words (`order`, `group`, `timestamp`, …) still
    need backticks to parse as identifiers in BQ.
    """
    select_sql = ", ".join(f"`{c}`" for c in req.select) if req.select else "*"
    table_ref = f"`{project_id}.{table_row.get('bucket') or ''}.{table_row.get('source_table') or req.table_id}`"
    sql = f"SELECT {select_sql} FROM {table_ref}"
    if safe_where:
        sql += f" WHERE {safe_where}"
    if req.order_by:
        sql += f" ORDER BY {', '.join(_quote_order_by_bq(e) for e in req.order_by)}"
    if req.limit:
        sql += f" LIMIT {int(req.limit)}"
    return sql


def estimate(conn, user, raw_request: dict, *, project_id: str, billing_project: str | None = None) -> dict:
    req = ScanRequest(**raw_request)
    repo = TableRegistryRepository(conn)
    row = repo.get(req.table_id)
    if not row:
        raise FileNotFoundError(req.table_id)
    if user.get("role") != "admin" and not can_access_table(user, req.table_id, conn):
        raise PermissionError(req.table_id)

    schema = _resolve_schema(conn, user, req.table_id, project_id)
    dialect = "bigquery" if (row.get("source_type") or "") == "bigquery" else "duckdb"

    # Validate WHERE and capture the comment-stripped fragment for splicing.
    safe_where = (
        safe_where_predicate(req.where, req.table_id, schema, dialect=dialect)
        if req.where else None
    )
    # Validate select columns exist (case-insensitive, matching order_by).
    if req.select:
        known = {c.lower() for c in schema}
        unknown = [c for c in req.select if c.lower() not in known]
        if unknown:
            raise ValueError(f"unknown columns: {unknown}")
    _validate_order_by(req.order_by, schema)

    if (row.get("source_type") or "") != "bigquery":
        return {
            "table_id": req.table_id,
            "estimated_scan_bytes": 0,
            "estimated_result_rows": None,
            "estimated_result_bytes": None,
            "bq_cost_estimate_usd": 0.0,
        }

    bq_sql = _build_bq_sql(row, project_id, req, safe_where=safe_where)
    scan_bytes = _bq_dry_run_bytes(billing_project or project_id, bq_sql)

    cost_per_tb = float(get_value("api", "scan", "bq_cost_per_tb_usd", default=5.0) or 5.0)
    cost = (scan_bytes / 1_099_511_627_776) * cost_per_tb  # 1 TiB = 2^40

    # Heuristic for result row/byte estimate. A row contains all selected
    # columns, so per-row bytes = sum of per-column estimates (NOT average).
    # If req.select is set, narrow to those columns; otherwise use full schema.
    cols_for_estimate = [schema[c] for c in (req.select or []) if c in schema] or list(schema.values())
    avg_row_bytes = max(1, sum(_avg_bytes_for_type(t) for t in cols_for_estimate))
    rows_est = scan_bytes // max(avg_row_bytes, 1)
    if req.limit:
        rows_est = min(rows_est, req.limit)

    return {
        "table_id": req.table_id,
        "estimated_scan_bytes": int(scan_bytes),
        "estimated_result_rows": int(rows_est),
        "estimated_result_bytes": int(rows_est * avg_row_bytes),
        "bq_cost_estimate_usd": round(cost, 4),
    }


def _avg_bytes_for_type(t: str) -> int:
    t = (t or "").upper()
    if t in ("INT64", "FLOAT64", "DATE", "TIMESTAMP", "DATETIME", "TIME"):
        return 8
    if t == "STRING":
        return 32  # rough average
    if t == "BYTES":
        return 64
    if t == "BOOL":
        return 1
    return 16


@router.post("/scan/estimate")
async def scan_estimate_endpoint(
    raw: dict,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    project_id = get_value("data_source", "bigquery", "project", default="") or ""
    billing_project = get_value("data_source", "bigquery", "billing_project", default="") or project_id
    try:
        return estimate(conn, user, raw, project_id=project_id, billing_project=billing_project)
    except WhereValidationError as e:
        raise HTTPException(status_code=400, detail={"error": "validator_rejected", "kind": e.kind, "details": e.detail or {}})
    except PermissionError:
        raise HTTPException(status_code=403, detail="not authorized")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="table not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# Module-level singleton (process-local quota state per spec §3.8). FastAPI
# dispatches sync handlers via a thread pool, so two concurrent first-time
# requests can both observe `_quota_singleton is None` and each construct a
# separate tracker; the second assignment wins and the first reference leaks
# split-brain quota state. Guard with an init lock + double-check.
import threading as _threading
_quota_init_lock = _threading.Lock()
_quota_singleton: QuotaTracker | None = None


def _build_quota_tracker() -> QuotaTracker:
    """Returns or constructs the process-local quota tracker (thread-safe)."""
    global _quota_singleton
    if _quota_singleton is not None:
        return _quota_singleton
    with _quota_init_lock:
        if _quota_singleton is None:
            _quota_singleton = QuotaTracker(
                max_concurrent_per_user=int(get_value("api", "scan", "max_concurrent_per_user", default=5) or 5),
                max_daily_bytes_per_user=int(get_value("api", "scan", "max_daily_bytes_per_user", default=53687091200) or 53687091200),
            )
    return _quota_singleton


def _max_result_bytes() -> int:
    return int(get_value("api", "scan", "max_result_bytes", default=2_147_483_648) or 2_147_483_648)


def _max_limit() -> int:
    return int(get_value("api", "scan", "max_limit", default=10_000_000) or 10_000_000)


def _run_bq_scan(project: str, sql: str) -> pa.Table:
    """Execute SQL via DuckDB BQ extension, return pyarrow Table."""
    from connectors.bigquery.auth import get_metadata_token

    token = get_metadata_token()
    conn = duckdb.connect(":memory:")
    try:
        conn.execute("INSTALL bigquery FROM community; LOAD bigquery;")
        escaped = token.replace("'", "''")
        conn.execute(f"CREATE OR REPLACE SECRET bq_s (TYPE bigquery, ACCESS_TOKEN '{escaped}')")
        # Use bigquery_query() since the SQL is already authored against the BQ jobs API
        return conn.execute(
            "SELECT * FROM bigquery_query(?, ?)",
            [project, sql],
        ).arrow()
    finally:
        conn.close()


def run_scan(
    conn: duckdb.DuckDBPyConnection,
    user: dict,
    raw_request: dict,
    *,
    project_id: str,
    quota: QuotaTracker,
    billing_project: str | None = None,
) -> bytes:
    """Validate → quota → execute → serialize. Returns Arrow IPC bytes.

    Raises:
        WhereValidationError, QuotaExceededError, FileNotFoundError, PermissionError, ValueError
    """
    req = ScanRequest(**raw_request)
    repo = TableRegistryRepository(conn)
    row = repo.get(req.table_id)
    if not row:
        raise FileNotFoundError(req.table_id)
    if user.get("role") != "admin" and not can_access_table(user, req.table_id, conn):
        raise PermissionError(req.table_id)

    if req.limit and req.limit > _max_limit():
        raise ValueError(f"limit {req.limit} exceeds max {_max_limit()}")

    schema = _resolve_schema(conn, user, req.table_id, project_id)
    dialect = "bigquery" if (row.get("source_type") or "") == "bigquery" else "duckdb"
    # Validate WHERE and capture the comment-stripped fragment for splicing.
    safe_where = (
        safe_where_predicate(req.where, req.table_id, schema, dialect=dialect)
        if req.where else None
    )
    if req.select:
        # Case-insensitive (BQ identifiers are case-insensitive; mixed-case
        # names from INFORMATION_SCHEMA.COLUMNS shouldn't 400-reject the
        # lowercased form a typical analyst writes).
        known = {c.lower() for c in schema}
        unknown = [c for c in req.select if c.lower() not in known]
        if unknown:
            raise ValueError(f"unknown columns: {unknown}")
    _validate_order_by(req.order_by, schema)

    source_type = row.get("source_type") or ""
    user_id = user.get("email") or "anon"

    with quota.acquire(user=user_id):
        if source_type != "bigquery":
            # Local source: query parquet directly. `source_type` extracted above
            # because `row["source_type"]` could be NULL for legacy registry rows
            # and `Path(...) / None` raises TypeError.
            from app.utils import get_data_dir
            parquet = (
                get_data_dir() / "extracts" / source_type / "data" / f"{req.table_id}.parquet"
            )
            local = duckdb.connect(":memory:")
            try:
                projection = ", ".join(f'"{c}"' for c in req.select) if req.select else "*"
                sql = f"SELECT {projection} FROM read_parquet(?)"
                if safe_where:
                    sql += f" WHERE {safe_where}"
                if req.order_by:
                    sql += f" ORDER BY {', '.join(_quote_order_by_duckdb(e) for e in req.order_by)}"
                if req.limit:
                    sql += f" LIMIT {int(req.limit)}"
                table = local.execute(sql, [str(parquet)]).arrow()
            finally:
                local.close()
        else:
            bq_sql = _build_bq_sql(row, project_id, req, safe_where=safe_where)
            table = _run_bq_scan(billing_project or project_id, bq_sql)

        ipc = arrow_table_to_ipc_bytes(table)

        # Enforce max_result_bytes guard (spec §3.4 step 8)
        if len(ipc) > _max_result_bytes():
            # Truncate by taking only as many rows as fit roughly
            # Simple heuristic: cap rows to estimated avg per max_bytes
            row_count = table.num_rows
            avg = max(1, len(ipc) // max(row_count, 1))
            keep = min(row_count, _max_result_bytes() // max(avg, 1))
            table = table.slice(0, keep)
            ipc = arrow_table_to_ipc_bytes(table)

        # Record bytes for daily quota
        quota.record_bytes(user=user_id, n=len(ipc))
        return ipc


@router.post("/scan")
async def scan_endpoint(
    raw: dict,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    project_id = get_value("data_source", "bigquery", "project", default="") or ""
    billing_project = get_value("data_source", "bigquery", "billing_project", default="") or project_id
    quota = _build_quota_tracker()
    try:
        ipc = run_scan(conn, user, raw, project_id=project_id, quota=quota, billing_project=billing_project)
        return Response(content=ipc, media_type=CONTENT_TYPE)
    except WhereValidationError as e:
        raise HTTPException(
            status_code=400,
            detail={"error": "validator_rejected", "kind": e.kind, "details": e.detail or {}},
        )
    except QuotaExceededError as e:
        raise HTTPException(
            status_code=429,
            detail={
                "error": "quota_exceeded",
                "kind": e.kind,
                "current": e.current,
                "limit": e.limit,
                "retry_after_seconds": e.retry_after_seconds,
            },
        )
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="table not found")
    except PermissionError:
        raise HTTPException(status_code=403, detail="not authorized")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
