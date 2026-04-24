"""GET /api/marketplace/zip - filtered marketplace as a deterministic ZIP."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response

from app.api.marketplace import _packager as packager
from app.api.marketplace.info import resolve_email
from app.auth.dependencies import get_optional_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/marketplace", tags=["marketplace"])


@router.get("/zip")
async def marketplace_zip(
    request: Request,
    email: str | None = Query(None),
    user: dict | None = Depends(get_optional_user),
) -> Response:
    resolved_email = resolve_email(user, email)

    if_none_match = request.headers.get("if-none-match", "").strip().strip('"')
    try:
        data, etag, _info = packager.build_zip(resolved_email)
    except FileNotFoundError:
        logger.warning("marketplace source unavailable at %s", packager.source_path())
        raise HTTPException(status_code=503, detail="marketplace source unavailable")

    if if_none_match and if_none_match == etag:
        logger.info("marketplace.zip 304 email=%s etag=%s", resolved_email, etag)
        return Response(status_code=304, headers={"ETag": f'"{etag}"'})

    headers = {
        "ETag": f'"{etag}"',
        "Content-Disposition": 'attachment; filename="agnes-marketplace.zip"',
    }
    logger.info(
        "marketplace.zip 200 email=%s etag=%s bytes=%d",
        resolved_email, etag, len(data),
    )
    return Response(content=data, media_type="application/zip", headers=headers)
