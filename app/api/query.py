"""Query endpoint — execute SQL against server DuckDB."""

import os
import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
import duckdb

from app.auth.dependencies import get_current_user, _get_db
from src.db import get_analytics_db_readonly
from src.rbac import get_accessible_tables
from src.repositories.table_registry import TableRegistryRepository

router = APIRouter(prefix="/api/query", tags=["query"])


class QueryRequest(BaseModel):
    sql: str
    limit: int = 1000


class QueryResponse(BaseModel):
    columns: list
    rows: list
    row_count: int
    truncated: bool = False


@router.post("", response_model=QueryResponse)
async def execute_query(
    request: QueryRequest,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Execute SQL against the server analytics DuckDB."""
    sql_lower = request.sql.strip().lower()

    # Block everything except SELECT
    blocked = [
        "drop ", "delete ", "insert ", "update ", "alter ", "create ",
        "copy ", "attach ", "detach ", "load ", "install ",
        "export ", "import ", "pragma ", "call ",
        # File access functions
        "read_csv", "read_json", "read_parquet", "read_text",
        "write_csv", "write_parquet", "read_blob", "read_ndjson",
        "parquet_scan", "parquet_metadata", "parquet_schema",
        "json_scan", "csv_scan",
        "query_table", "iceberg_scan", "delta_scan",
        "glob(", "list_files",
        "'/", '"/','http://', 'https://', 's3://', 'gcs://',
        # DuckDB metadata (leaks schema info regardless of RBAC)
        "information_schema", "duckdb_tables", "duckdb_columns",
        "duckdb_databases", "duckdb_settings", "duckdb_functions",
        "duckdb_views", "duckdb_indexes", "duckdb_schemas",
        "pragma_table_info", "pragma_storage_info",
        # Relative path traversal
        "'../", '"../',
        # Multiple statements
        ";",
    ]
    if any(keyword in sql_lower for keyword in blocked):
        raise HTTPException(status_code=400, detail="Only single SELECT queries are allowed")

    # Accept any whitespace (newline, tab, space) after the keyword so
    # multi-line SQL doesn't 400 on `SELECT\n  col, ...`.
    import re as _re
    if not _re.match(r"^(select|with)\s", sql_lower):
        raise HTTPException(status_code=400, detail="Query must start with SELECT or WITH")

    # Get allowed tables for this user
    allowed = get_accessible_tables(user, conn)

    analytics = get_analytics_db_readonly()
    try:
        if allowed is not None:  # None = admin, sees all
            # Get all views in analytics DB
            all_views = {row[0] for row in analytics.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_type='VIEW'"
            ).fetchall()}

            # Check if query references any forbidden tables (word-boundary match)
            forbidden = all_views - set(allowed)
            for table in forbidden:
                pattern = r'\b' + re.escape(table.lower()) + r'\b'
                if re.search(pattern, sql_lower):
                    raise HTTPException(status_code=403, detail=f"Access denied to table '{table}'")

        # Open in read-only mode for extra safety
        result = analytics.execute(request.sql).fetchmany(request.limit + 1)
        columns = [desc[0] for desc in analytics.description] if analytics.description else []
        truncated = len(result) > request.limit
        rows = result[:request.limit]
        # Convert to serializable types
        serializable_rows = []
        for row in rows:
            serializable_rows.append([
                str(v) if v is not None and not isinstance(v, (int, float, bool, str)) else v
                for v in row
            ])
        return QueryResponse(
            columns=columns,
            rows=serializable_rows,
            row_count=len(serializable_rows),
            truncated=truncated,
        )
    except HTTPException:
        raise
    except Exception as e:
        # If DuckDB raised "Table … does not exist" for a referenced name,
        # check whether that name belongs to a registry row in
        # `query_mode='materialized'` that hasn't yet been materialized in
        # this instance's analytics.duckdb. Materialized rows produce a
        # parquet at `${DATA_DIR}/extracts/<source>/data/<id>.parquet` but
        # the orchestrator is `_meta`-driven and only creates master views
        # for connectors that emit `_meta` rows — so on a fresh instance
        # (or before the first scheduler tick) the master view doesn't
        # exist yet and the operator gets a confusing "table does not
        # exist" with no path forward. Surface a materialize-aware hint
        # instead of DuckDB's bare error.
        msg = str(e)
        helpful = _materialized_hint_for_query_error(conn, request.sql, msg)
        if helpful:
            raise HTTPException(status_code=400, detail=helpful)
        raise HTTPException(status_code=400, detail=f"Query error: {msg}")
    finally:
        analytics.close()


def _materialized_hint_for_query_error(
    conn: duckdb.DuckDBPyConnection, sql: str, error_msg: str,
) -> Optional[str]:
    """Return a materialize-aware error message if the failed query
    references a registry row whose `query_mode='materialized'` and which
    has no master view in analytics.duckdb yet, OR ``None`` to fall back
    to DuckDB's raw error.

    The detection scans each materialized row's id/name against the SQL
    text; a hit means the operator picked a name that exists in the
    registry but isn't queryable in this instance. The hint is the same
    in both arms of the OR — it tells them what the table needs and what
    they can do today (`da sync` or query `bq."dataset"."table"`
    directly using the bucket/source_table from the registry row).
    """
    # Cheap fast-path — only inspect the registry when DuckDB's error
    # actually mentions a missing table. Avoids registry round-trip on
    # every parse/cast/permission failure.
    el = error_msg.lower()
    if "does not exist" not in el and "table with name" not in el:
        return None
    try:
        repo = TableRegistryRepository(conn)
        rows = repo.list_all()
    except Exception:
        # Registry read failed for whatever reason — don't compound the
        # error response by hiding the original DuckDB message.
        return None
    sql_l = sql.lower()
    for r in rows:
        if (r.get("query_mode") or "") != "materialized":
            continue
        # Match by id or by name; either could appear in the SQL.
        candidates = {r.get("id"), r.get("name")}
        for cand in candidates:
            if not cand:
                continue
            cand_l = str(cand).lower()
            # Word-boundary-ish check — `\b` doesn't match `.` so
            # `bq.dataset.cand` would still hit, which is fine for the
            # hint path (the operator is referring to the same table).
            if re.search(r"\b" + re.escape(cand_l) + r"\b", sql_l):
                return _build_materialized_hint(r)
    return None


def _build_materialized_hint(row: dict) -> str:
    """Format the user-facing hint for a materialized row that's not yet
    queryable. Includes the table id, the bucket/source_table when the
    row carries them, and concrete operator next steps."""
    tid = row.get("id") or row.get("name") or "<unknown>"
    bucket = row.get("bucket")
    source_table = row.get("source_table")
    direct_hint = ""
    if bucket and source_table:
        # BigQuery: `bq."dataset"."table"`; Keboola: `kbc."bucket"."table"`.
        # Pick the alias by source_type so the hint is copy-pasteable.
        alias = "bq" if (row.get("source_type") or "") == "bigquery" else "kbc"
        direct_hint = (
            f' or query the source directly via {alias}."{bucket}".'
            f'"{source_table}"'
        )
    return (
        f"Table {tid!r} is registered as query_mode='materialized' but is "
        f"not yet materialized in this instance's analytics views. Run "
        f"`da sync` (or wait for the scheduler tick / hit POST "
        f"/api/sync/trigger) to materialize the parquet"
        f"{direct_hint}."
    )
