"""Data download endpoint — streaming parquet files."""

import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
import duckdb

from app.auth.dependencies import get_current_user, _get_db
from app.utils import get_data_dir as _get_data_dir
from src.audit_helpers import client_kind_from_user
from src.identifier_validation import _SAFE_QUOTED_IDENTIFIER
from src.rbac import can_access_table
from src.repositories.audit import AuditRepository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/data", tags=["data"])


@router.get("/{table_id}/check-access")
async def check_access(
    table_id: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Lightweight RBAC probe used by Caddy's ``forward_auth`` directive
    to gate file_server-served parquet downloads without involving the
    app's request workers in the bulk byte transfer.

    Returns HTTP 204 No Content when the caller has read access to
    ``table_id``; HTTP 403 (via ``can_access_table`` returning False)
    otherwise. Caddy treats 2xx as authorized and forwards the request
    to its own ``file_server`` block; non-2xx is returned to the client
    verbatim.

    Why a separate endpoint and not just ``HEAD /download``: ``HEAD`` on
    the FileResponse-based ``download`` handler still opens the file and
    runs stat() to populate Content-Length / ETag. ``forward_auth`` calls
    this endpoint on every request, so the per-call cost matters; a pure
    RBAC check is ~1 ms while a HEAD path involves filesystem walks
    (``rglob`` for the parquet across source subdirs).
    """
    t0 = time.monotonic()
    resource = f"table:{table_id}"[:256]
    if not _SAFE_QUOTED_IDENTIFIER.match(table_id):
        try:
            AuditRepository(conn).log(
                user_id=user.get("id"),
                action="data.access_check",
                resource=resource,
                params={"granted": False,
                        "duration_ms": int((time.monotonic() - t0) * 1000),
                        "error": "invalid_table_id"},
                result="error.404",
                client_kind=client_kind_from_user(user),
            )
        except Exception:
            logger.exception("audit_log write failed for data.access_check (invalid id); continuing")
        raise HTTPException(status_code=404, detail="Table not found")
    granted = can_access_table(user, table_id, conn)
    try:
        AuditRepository(conn).log(
            user_id=user.get("id"),
            action="data.access_check",
            resource=resource,
            params={
                "granted": granted,
                "duration_ms": int((time.monotonic() - t0) * 1000),
            },
            result="success" if granted else "error.403",
            client_kind=client_kind_from_user(user),
        )
    except Exception:
        logger.exception("audit_log write failed for data.access_check; continuing")
    if not granted:
        raise HTTPException(status_code=403, detail="Access denied to this table")
    return Response(status_code=204)


@router.get("/{table_id}/download")
async def download_table(
    table_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Stream a parquet file for download. Supports ETag for caching.

    On Caddy-fronted deployments the matching Caddyfile rule intercepts
    ``GET /api/data/{table_id}/download``, calls ``check-access`` via
    ``forward_auth``, and serves the parquet directly via ``file_server``
    — bypassing this handler entirely. This handler stays as the
    canonical fallback for non-Caddy deployments (dev `docker compose
    up`, alternative reverse proxies, direct :8000 access) where the
    bulk transfer goes through uvicorn.
    """
    # Reject unsafe table_id before any filesystem or DB operations.
    # Use the relaxed quoted-identifier check that allows dots and hyphens
    # (Keboola table IDs like "in.c-crm.orders") while still blocking
    # path-traversal characters (/, .., \) and quote/control chars.
    if not _SAFE_QUOTED_IDENTIFIER.match(table_id):
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
        return Response(status_code=304)

    try:
        AuditRepository(conn).log(
            user_id=user.get("id"),
            action="data.download",
            resource=f"table:{table_id}"[:256],
            params={"bytes": stat.st_size, "format": "parquet"},
            result="success",
            client_kind=client_kind_from_user(user),
        )
    except Exception:
        logger.exception("audit_log write failed for data.download; continuing")

    return FileResponse(
        path=file_path,
        filename=f"{table_id}.parquet",
        media_type="application/octet-stream",
        headers={"ETag": etag},
    )
