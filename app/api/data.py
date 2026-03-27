"""Data download endpoint — streaming parquet files."""

import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from app.auth.dependencies import get_current_user

router = APIRouter(prefix="/api/data", tags=["data"])


def _get_data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", "./data"))


@router.get("/{table_id}/download")
async def download_table(
    table_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    """Stream a parquet file for download. Supports ETag for caching."""
    data_dir = _get_data_dir()
    parquet_dir = data_dir / "src_data" / "parquet"

    # Find the parquet file (may be in a subfolder)
    candidates = list(parquet_dir.rglob(f"{table_id}.parquet"))
    if not candidates:
        # Try with folder structure: folder/table.parquet
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
