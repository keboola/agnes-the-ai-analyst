"""FastAPI router for the aggregated marketplace endpoint.

Four GET routes:
  - /marketplace/info                              → JSON summary (diagnostic / admin)
  - /marketplace.zip                               → ZIP download with ETag / If-None-Match
  - /marketplace.git/                              → JSON manifest at the
    bare URL Claude Code's `plugin marketplace add <https-url>` actually
    fetches (it GETs the URL verbatim, doesn't append `.claude-plugin/...`).
  - /marketplace.git/.claude-plugin/marketplace.json → same JSON, kept for
    callers that follow the standard `.claude-plugin/marketplace.json`
    convention (curl, future Claude Code versions).

All four registered before the `/marketplace.git` mount in main.py so
FastAPI matches the explicit routes before falling through to the git
smart-HTTP WSGI app — git's own paths (`info/refs`, `git-upload-pack`)
land on the mount and the smart-HTTP clone keeps working.

All gated by the existing `get_current_user` dependency (Bearer PAT or cookie).
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


@router.get("/marketplace.git/")
@router.get("/marketplace.git/.claude-plugin/marketplace.json")
async def marketplace_json(
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
) -> JSONResponse:
    return JSONResponse(packager.build_marketplace_json(conn, user))


@router.get("/marketplace.zip")
async def marketplace_zip(
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
) -> Response:
    if_none_match = request.headers.get("if-none-match", "").strip().strip('"')
    # Resolve the etag first — this lets a 304 short-circuit before we read
    # every plugin file off disk and run ZIP_DEFLATED. Hot path on every
    # Claude Code SessionStart.
    etag, plugins = packager.compute_etag_for_user(conn, user)
    if if_none_match and if_none_match == etag:
        return Response(status_code=304, headers={"ETag": f'"{etag}"'})

    data, _ = packager.build_zip(conn, user, plugins=plugins, etag=etag)
    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "ETag": f'"{etag}"',
            "Content-Disposition": 'attachment; filename="agnes-marketplace.zip"',
        },
    )
