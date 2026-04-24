"""GET /api/marketplace/info - JSON describing the caller's allowed plugins."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from app.api.marketplace import _auth, _packager as packager
from app.auth.dependencies import get_optional_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/marketplace", tags=["marketplace"])


def resolve_email(user: dict | None, email_param: str | None) -> str:
    """Prefer the authenticated user's email; fall back to `?email=` only if
    MARKETPLACE_ALLOW_EMAIL_AUTH=1 is set (migration-only escape hatch).

    Raises HTTPException(401) if neither is usable.
    """
    if user and user.get("email"):
        return user["email"]
    if email_param:
        if not _auth.allow_email_auth():
            raise HTTPException(
                status_code=401,
                detail="email query parameter requires MARKETPLACE_ALLOW_EMAIL_AUTH=1",
            )
        return email_param
    raise HTTPException(status_code=401, detail="authentication required")


@router.get("/info")
async def marketplace_info(
    email: str | None = Query(None),
    user: dict | None = Depends(get_optional_user),
) -> JSONResponse:
    resolved_email = resolve_email(user, email)
    try:
        info = packager.build_info(resolved_email)
    except FileNotFoundError:
        logger.warning("marketplace source unavailable at %s", packager.source_path())
        raise HTTPException(status_code=503, detail="marketplace source unavailable")
    logger.info(
        "marketplace.info email=%s etag=%s plugins=%d",
        resolved_email, info["etag"], len(info["plugins"]),
    )
    return JSONResponse(info)
