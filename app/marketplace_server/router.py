"""FastAPI router for the aggregated marketplace endpoint.

Two GET routes:
  - /marketplace/info   → JSON summary (diagnostic / admin)
  - /marketplace.zip    → ZIP download with ETag / If-None-Match

Both gated by the existing `get_current_user` dependency (Bearer PAT or cookie).
The git smart-HTTP channel lives in git_router.py and is mounted separately
because it needs raw WSGI I/O that FastAPI doesn't model natively.
"""

from __future__ import annotations

import logging

import duckdb
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from app.auth.dependencies import _get_db, get_current_user
from app.marketplace_server import packager

logger = logging.getLogger(__name__)

router = APIRouter(tags=["marketplace"])


@router.get("/marketplace/info")
async def marketplace_info(
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
) -> JSONResponse:
    info = packager.build_info(conn, user)
    return JSONResponse(info)


@router.get("/marketplace.zip")
async def marketplace_zip(
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
) -> Response:
    if_none_match = request.headers.get("if-none-match", "").strip().strip('"')

    # Compute the ETag first (DB query + file hashes) so we can short-circuit
    # with 304 before paying for file collection + ZIP compression.
    etag = packager.compute_etag_for_user(conn, user)
    if if_none_match and if_none_match == etag:
        return Response(status_code=304, headers={"ETag": f'"{etag}"'})

    data, etag = packager.build_zip(conn, user)
    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "ETag": f'"{etag}"',
            "Content-Disposition": 'attachment; filename="agnes-marketplace.zip"',
        },
    )
