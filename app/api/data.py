"""Data download endpoint — streaming parquet files."""

import logging
import re
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
import duckdb

from app.auth.dependencies import get_current_user, _get_db
from app.utils import get_data_dir as _get_data_dir
from src.audit_helpers import identity_for_audit, client_kind_from_user
from src.identifier_validation import _SAFE_QUOTED_IDENTIFIER
from src.rbac import can_access_table

from src.repositories import (
    audit_repo,
)
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/data", tags=["data"])

# A single path segment of a partitioned-table part relpath. Allows hive
# partition dirs (``month=2026-06``), flat partition keys (``2025_11.parquet``)
# and ``data.parquet`` — first char alnum/underscore, then alnum/_/./=/-.
# Deliberately excludes ``/`` and ``\`` (segments are split on ``/`` first).
_SAFE_PART_SEGMENT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.=-]*$")


def _resolve_part_path(extracts_dir: Path, table_id: str, part: str) -> Path | None:
    """Map a manifest ``part`` relpath to a safe on-disk file under the
    table's data directory, or ``None`` if it is unsafe / not found.

    Partitioned tables live at ``extracts/<source>/data/<table_id>/<part>``
    (Jira hive ``month=*/data.parquet``, Keboola flat ``<key>.parquet``).
    Path traversal is blocked three ways: reject absolute / ``\\`` / NUL /
    empty / ``.``|``..`` segments up front; require every segment to match
    ``_SAFE_PART_SEGMENT``; and finally assert the resolved path stays under
    the (resolved) table dir. Only an existing regular file is returned.
    """
    if not part or part.startswith("/") or "\\" in part or "\x00" in part:
        return None
    segments = part.split("/")
    if any(s in ("", ".", "..") for s in segments):
        return None
    if not all(_SAFE_PART_SEGMENT.match(s) for s in segments):
        return None
    if not extracts_dir.exists():
        return None
    for table_dir in extracts_dir.glob(f"*/data/{table_id}"):
        if not table_dir.is_dir():
            continue
        base = table_dir.resolve()
        candidate = (base / part).resolve()
        if (candidate == base or base in candidate.parents) and candidate.is_file():
            return candidate
    return None


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
            audit_repo().log(
                user_id=identity_for_audit(user)[0],
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
        audit_repo().log(
            user_id=identity_for_audit(user)[0],
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
        from src.rbac import table_not_in_stack_message
        raise HTTPException(
            status_code=403, detail=table_not_in_stack_message(table_id),
        )
    return Response(status_code=204)


@router.get("/{table_id}/download")
async def download_table(
    table_id: str,
    request: Request,
    part: str | None = None,
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
        from src.rbac import table_not_in_stack_message
        raise HTTPException(
            status_code=403, detail=table_not_in_stack_message(table_id),
        )

    data_dir = _get_data_dir()
    extracts_dir = data_dir / "extracts"

    if part is not None:
        # Partitioned-table part download (partitioned distribution): serve
        # one part file under the table's data dir. `_resolve_part_path`
        # blocks traversal and confirms the file exists.
        file_path = _resolve_part_path(extracts_dir, table_id, part)
        if file_path is None:
            raise HTTPException(status_code=404, detail="Part not found")
    else:
        # Single-file table: v2 extract.duckdb path, then legacy layout.
        candidates = list(extracts_dir.rglob(f"data/{table_id}.parquet")) if extracts_dir.exists() else []
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

    audit_params = {"bytes": stat.st_size, "format": "parquet"}
    if part is not None:
        audit_params["part"] = part
    try:
        audit_repo().log(
            user_id=identity_for_audit(user)[0],
            action="data.download",
            resource=f"table:{table_id}"[:256],
            params=audit_params,
            result="success",
            client_kind=client_kind_from_user(user),
        )
    except Exception:
        logger.exception("audit_log write failed for data.download; continuing")

    return FileResponse(
        path=file_path,
        filename=file_path.name if part is not None else f"{table_id}.parquet",
        media_type="application/octet-stream",
        headers={"ETag": etag},
    )
