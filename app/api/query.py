"""Query endpoint — execute SQL against server DuckDB."""

import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
import duckdb

from app.auth.dependencies import get_current_user, _get_db
from src.db import get_analytics_db_readonly
from src.rbac import get_accessible_tables

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
        # Multiple statements
        ";",
    ]
    if any(keyword in sql_lower for keyword in blocked):
        raise HTTPException(status_code=400, detail="Only single SELECT queries are allowed")

    if not sql_lower.startswith("select ") and not sql_lower.startswith("with "):
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

            # Check if query references any forbidden tables
            forbidden = all_views - set(allowed)
            for table in forbidden:
                if table.lower() in sql_lower:
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
        raise HTTPException(status_code=400, detail=f"Query error: {str(e)}")
    finally:
        analytics.close()
