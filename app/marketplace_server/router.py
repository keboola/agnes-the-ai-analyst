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
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from app.auth.dependencies import _get_db, get_current_user
from app.marketplace_server import cowork_packager, packager
from src import marketplace_filter

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


@router.get("/marketplace/cowork/{prefixed_name}.zip")
async def cowork_plugin_zip(
    prefixed_name: str,
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
) -> Response:
    """Download a single plugin packaged for Claude Desktop's Cowork upload.

    Cowork expects one plugin per zip, at the zip root (no marketplace.json
    wrapper) and run through a stricter validator than Claude Code's — see
    ``cowork_packager`` for the transforms. RBAC is enforced implicitly:
    ``resolve_user_marketplace`` only returns plugins the caller is granted,
    so an unknown / ungranted ``prefixed_name`` is simply absent → 404.
    """
    plugins = marketplace_filter.resolve_user_marketplace(conn, user)
    match = next((p for p in plugins if p["prefixed_name"] == prefixed_name), None)
    if match is None:
        raise HTTPException(status_code=404, detail="plugin_not_found")

    etag = cowork_packager.compute_cowork_etag(match)
    if_none_match = request.headers.get("if-none-match", "").strip().strip('"')
    if if_none_match and if_none_match == etag:
        return Response(status_code=304, headers={"ETag": f'"{etag}"'})

    data, _ = cowork_packager.build_cowork_zip(match)
    # Filename uses the served prefixed_name (e.g. groupon-marketplace-grpn.zip)
    # — unique across marketplaces, and what the user sees in the catalog.
    filename = f"{prefixed_name}.zip"
    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "ETag": f'"{etag}"',
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )
