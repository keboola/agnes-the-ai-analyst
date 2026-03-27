"""Query endpoint — execute SQL against server DuckDB."""

import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.dependencies import get_current_user
from src.db import get_analytics_db

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
):
    """Execute SQL against the server analytics DuckDB."""
    sql_lower = request.sql.strip().lower()

    # Block everything except SELECT
    blocked = [
        "drop ", "delete ", "insert ", "update ", "alter ", "create ",
        "copy ", "attach ", "detach ", "load ", "install ",
        "export ", "import ", "pragma ",
        # File access functions
        "read_csv", "read_json", "read_parquet(", "read_text",
        "write_csv", "write_parquet",
        # Multiple statements
        ";",
    ]
    if any(keyword in sql_lower for keyword in blocked):
        raise HTTPException(status_code=400, detail="Only single SELECT queries are allowed")

    if not sql_lower.startswith("select ") and not sql_lower.startswith("with "):
        raise HTTPException(status_code=400, detail="Query must start with SELECT or WITH")

    conn = get_analytics_db()
    try:
        # Open in read-only mode for extra safety
        result = conn.execute(request.sql).fetchmany(request.limit + 1)
        columns = [desc[0] for desc in conn.description] if conn.description else []
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
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Query error: {str(e)}")
    finally:
        conn.close()
