"""Per-table outbound MCP tools (RFC #461 §7).

For every row in ``table_registry`` we expose a single REST endpoint
``POST /api/mcp/query-table/{table_id}`` that runs a constrained
SELECT against the local DuckDB view backing that table. The filter
shape is intentionally simple — a flat ``{column: value}`` equality
dict — so the schema stays guessable by AI clients and the SQL we
build is trivially safe to parameterize.

* RBAC: the caller must have access to ``ResourceType.TABLE`` for
  ``table_id`` (admin short-circuits via ``can_access``).
* Validation: every filter key must be present in the table's
  current schema (read via DuckDB ``DESCRIBE``); unknown keys
  return 400 with a list of allowed columns so the AI can correct.
* Limit: capped at ``MAX_LIMIT`` rows per call so a poorly-formed
  call can't smoke a large table.

The intention is that a stdio / SSE MCP server later surfaces one
FastMCP tool per ``table_registry`` row that proxies through here,
matching how passthrough tools surface today. That generator is a
small follow-up — it reads the catalog and dynamically registers
named tools. For now the REST endpoint is the source of truth.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import duckdb
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth.access import can_access
from app.auth.dependencies import _get_db, get_current_user
from app.resource_types import ResourceType
from src.db import get_analytics_db
from src.repositories.table_registry import TableRegistryRepository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mcp/query-table", tags=["mcp-per-table"])


MAX_LIMIT = 1000


class TableQueryRequest(BaseModel):
    filter: Dict[str, Any] = Field(default_factory=dict)
    limit: int = 100


class TableQueryResponse(BaseModel):
    table_id: str
    rows: List[Dict[str, Any]]
    row_count: int
    columns: List[str]
    truncated: bool


def _column_names(analytics_conn: duckdb.DuckDBPyConnection, table_view_name: str) -> List[str]:
    """Best-effort column lookup for the view backing ``table_view_name``.

    Uses ``DESCRIBE`` which works on DuckDB views the orchestrator
    creates over attached extract.duckdb files. Returns an empty list
    on any error so the caller can 400 with a clear message rather
    than 500-ing on a missing view.
    """
    try:
        rows = analytics_conn.execute(f'DESCRIBE "{table_view_name}"').fetchall()
        return [r[0] for r in rows]
    except Exception:
        return []


@router.post("/{table_id}", response_model=TableQueryResponse)
async def query_table(
    table_id: str,
    body: TableQueryRequest,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
) -> TableQueryResponse:
    """Run a constrained filter+limit query against a registered table.

    Pure SELECT — no aggregation, no projection control. AI clients
    that need richer queries should use the generic ``query(sql)``
    surface from Monika's cowork foundation; this endpoint is the
    "fast path" for the common per-table lookup.
    """
    if body.limit <= 0:
        raise HTTPException(status_code=400, detail="limit must be > 0")
    limit = min(body.limit, MAX_LIMIT)
    truncated = body.limit > MAX_LIMIT

    # Registry lookup + RBAC. Internal tables (agnes_sessions / _usage / _audit)
    # are implicitly granted to every authenticated user via can_access's
    # internal-table short-circuit.
    tables_repo = TableRegistryRepository(conn)
    table = tables_repo.get(table_id)
    if table is None:
        raise HTTPException(status_code=404, detail="table_not_found")
    if not can_access(user["id"], ResourceType.TABLE.value, table_id, conn):
        raise HTTPException(status_code=403, detail=f"no grant on table {table_id!r}")

    # The orchestrator creates views in analytics.duckdb under the
    # table_registry id. Use the id rather than .name — the view
    # always exists under the id; .name is a UX label that may collide.
    view_name = table_id

    # Reuse the pooled analytics connection — DuckDB rejects opening a
    # second read-only connection alongside a writer (the orchestrator
    # holds one for rebuild_*). Safety against accidental mutation comes
    # from this endpoint's own SQL builder: SELECT only, parameterized,
    # plus the column allow-list below.
    analytics_conn = get_analytics_db()
    columns = _column_names(analytics_conn, view_name)
    if not columns:
        raise HTTPException(
            status_code=409,
            detail=f"table view {view_name!r} is not present in analytics.duckdb (sync may not have run yet)",
        )

    unknown_keys = [k for k in body.filter.keys() if k not in columns]
    if unknown_keys:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "unknown_filter_columns",
                "unknown": unknown_keys,
                "allowed": columns,
            },
        )

    sql, params = _build_select(view_name, body.filter, limit)
    rows = analytics_conn.execute(sql, params).fetchdf()
    # Coerce dataframe scalars to native Python types — fastapi's JSON
    # encoder doesn't know about numpy / pandas scalars.
    result_records = _coerce_records(rows.to_dict(orient="records"))

    return TableQueryResponse(
        table_id=table_id,
        rows=result_records,
        row_count=len(result_records),
        columns=columns,
        truncated=truncated,
    )


def _build_select(
    view_name: str,
    filter_dict: Dict[str, Any],
    limit: int,
) -> tuple[str, List[Any]]:
    """Build a parameterized SELECT * FROM view WHERE col = ? ... LIMIT N.

    The view name is interpolated as an identifier (validated by the
    table_registry id constraint upstream); filter columns are
    quoted with double quotes; values go through DuckDB's positional
    parameter binding which prevents SQL injection on the value side.
    """
    where_parts: List[str] = []
    params: List[Any] = []
    for col, val in filter_dict.items():
        where_parts.append(f'"{col}" = ?')
        params.append(val)

    where_clause = " WHERE " + " AND ".join(where_parts) if where_parts else ""
    sql = f'SELECT * FROM "{view_name}"{where_clause} LIMIT {int(limit)}'
    return sql, params


def _coerce_records(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert pandas / numpy scalar types to JSON-friendly Python natives.

    DuckDB returns timestamps as pd.Timestamp and ints as np.int64;
    pydantic + json both handle native datetime / int just fine but
    not the pandas wrappers. ISO-format timestamps, int() the numerics,
    leave everything else alone.
    """
    out: List[Dict[str, Any]] = []
    for rec in records:
        row: Dict[str, Any] = {}
        for k, v in rec.items():
            if v is None:
                row[k] = None
            elif hasattr(v, "isoformat"):  # datetime / pd.Timestamp
                row[k] = v.isoformat()
            elif hasattr(v, "item"):  # numpy scalar
                try:
                    row[k] = v.item()
                except Exception:
                    row[k] = str(v)
            else:
                row[k] = v
        out.append(row)
    return out
