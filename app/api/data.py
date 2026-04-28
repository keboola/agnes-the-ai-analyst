"""Data download endpoint — streaming parquet files."""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
import duckdb

from app.auth.dependencies import get_current_user, _get_db
from app.utils import get_data_dir as _get_data_dir
from src.db import _SAFE_IDENTIFIER
from src.rbac import can_access_table

router = APIRouter(prefix="/api/data", tags=["data"])


@router.get("/{table_id}/download")
async def download_table(
    table_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Stream a parquet file for download. Supports ETag for caching."""
    # Reject unsafe table_id before any filesystem or DB operations
    if not _SAFE_IDENTIFIER.match(table_id):
        raise HTTPException(status_code=404, detail="Table not found")
    # Check access FIRST
    if not can_access_table(user, table_id, conn):
        raise HTTPException(status_code=403, detail="Access denied to this table")

    data_dir = _get_data_dir()

    # Search in extracts directory (v2 extract.duckdb architecture)
    extracts_dir = data_dir / "extracts"
    candidates = list(extracts_dir.rglob(f"data/{table_id}.parquet")) if extracts_dir.exists() else []

    # Fallback to legacy path for backward compatibility
    if not candidates:
        parquet_dir = data_dir / "src_data" / "parquet"
        candidates = list(parquet_dir.rglob(f"{table_id}.parquet"))
        if not candidates:
            candidates = list(parquet_dir.rglob(f"*/{table_id}.parquet"))

    if not candidates:
        raise HTTPException(status_code=404, detail=f"Table '{table_id}' not found")

    file_path = candidates[0]

    # ETag support
    stat = file_path.stat()
    etag = f'"{stat.st_mtime_ns}"'
    if_none_match = request.headers.get("if-none-match")
    if if_none_match == etag:
        from starlette.responses import Response
        return Response(status_code=304)

    return FileResponse(
        path=file_path,
        filename=f"{table_id}.parquet",
        media_type="application/octet-stream",
        headers={"ETag": etag},
    )
