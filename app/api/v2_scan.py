"""POST /api/v2/scan and POST /api/v2/scan/estimate (spec §3.4 + §3.5)."""

from __future__ import annotations
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
import duckdb

from app.auth.dependencies import get_current_user, _get_db
from app.instance_config import get_value
from src.rbac import can_access_table
from src.repositories.table_registry import TableRegistryRepository
from app.api.where_validator import (
    validate_where, WhereValidationError,
)
from app.api.v2_schema import build_schema  # reused for column resolution

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


def _build_bq_sql(table_row: dict, project_id: str, req: ScanRequest) -> str:
    select_sql = ", ".join(req.select) if req.select else "*"
    table_ref = f"`{project_id}.{table_row.get('bucket') or ''}.{table_row.get('source_table') or req.table_id}`"
    sql = f"SELECT {select_sql} FROM {table_ref}"
    if req.where:
        sql += f" WHERE {req.where}"
    if req.order_by:
        sql += f" ORDER BY {', '.join(req.order_by)}"
    if req.limit:
        sql += f" LIMIT {int(req.limit)}"
    return sql


def estimate(conn, user, raw_request: dict, *, project_id: str) -> dict:
    req = ScanRequest(**raw_request)
    repo = TableRegistryRepository(conn)
    row = repo.get(req.table_id)
    if not row:
        raise FileNotFoundError(req.table_id)
    if user.get("role") != "admin" and not can_access_table(user, req.table_id, conn):
        raise PermissionError(req.table_id)

    schema = _resolve_schema(conn, user, req.table_id, project_id)

    # Validate WHERE first
    if req.where:
        validate_where(req.where, req.table_id, schema)
    # Validate select columns exist
    if req.select:
        unknown = [c for c in req.select if c not in schema]
        if unknown:
            raise ValueError(f"unknown columns: {unknown}")

    if (row.get("source_type") or "") != "bigquery":
        return {
            "table_id": req.table_id,
            "estimated_scan_bytes": 0,
            "estimated_result_rows": None,
            "estimated_result_bytes": None,
            "bq_cost_estimate_usd": 0.0,
        }

    bq_sql = _build_bq_sql(row, project_id, req)
    scan_bytes = _bq_dry_run_bytes(project_id, bq_sql)

    cost_per_tb = float(get_value("api", "scan", "bq_cost_per_tb_usd", default=5.0) or 5.0)
    cost = (scan_bytes / 1_099_511_627_776) * cost_per_tb  # 1 TiB = 2^40

    # Heuristic for result row/byte estimate
    avg_row_bytes = max(1, sum(_avg_bytes_for_type(t) for t in schema.values()) // max(1, len(schema)))
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
    try:
        return estimate(conn, user, raw, project_id=project_id)
    except WhereValidationError as e:
        raise HTTPException(status_code=400, detail={"error": "validator_rejected", "kind": e.kind, "details": e.detail or {}})
    except PermissionError:
        raise HTTPException(status_code=403, detail="not authorized")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="table not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
