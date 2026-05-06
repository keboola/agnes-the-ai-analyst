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

``/marketplace/info`` and ``/marketplace.zip`` use ``get_current_user``
(Bearer PAT or cookie). The two ``marketplace.json`` routes use
``_user_for_marketplace_json`` which additionally accepts HTTP Basic
auth — Claude Code's `plugin marketplace add https://x:<PAT>@host/...`
sends Basic with the PAT in the password field, mirroring git clone.

The git smart-HTTP channel lives in git_router.py and is mounted separately
because it needs raw WSGI I/O that FastAPI doesn't model natively.
"""

from __future__ import annotations

import logging
from typing import Optional

import duckdb
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response

from app.auth.dependencies import _attach_admin_flag, _get_db, get_current_user
from app.marketplace_server import git_router, packager

logger = logging.getLogger(__name__)

router = APIRouter(tags=["marketplace"])


async def _user_for_marketplace_json(
    request: Request,
    authorization: Optional[str] = Header(None),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
) -> dict:
    """Auth dependency for the bare marketplace.json route.

    Claude Code's `plugin marketplace add https://x:<PAT>@host/...` GETs the
    URL the same way `git clone` would — credentials embedded in user-info
    arrive as HTTP Basic, with the PAT in the password field. The standard
    `get_current_user` only reads Bearer + cookie, so Basic-bearing requests
    would otherwise 401 here despite carrying a valid PAT.

    We try Basic first (Claude's actual path), and fall back to delegating
    to ``get_current_user`` so curl-with-Bearer and cookie-authenticated UI
    sessions keep working unchanged. Same PAT resolution + admin-flag
    attachment as everywhere else — Basic just changes how the token gets
    extracted off the wire.
    """
    token = git_router.token_from_basic_auth(authorization)
    if token:
        from app.auth.pat_resolver import resolve_token_to_user
        user, _reason = resolve_token_to_user(conn, token, request)
        if user:
            _attach_admin_flag(user, conn)
            return user
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )
    return await get_current_user(request=request, authorization=authorization, conn=conn)


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
    user: dict = Depends(_user_for_marketplace_json),
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
