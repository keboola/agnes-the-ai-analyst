"""REST endpoints for the setup-page banner.

- GET  /api/admin/setup-banner         : raw content + audit info (admin)
- PUT  /api/admin/setup-banner         : set banner (admin)
- DELETE /api/admin/setup-banner       : clear banner (admin)
- POST /api/admin/setup-banner/preview : preview arbitrary content (admin)
"""

import datetime
from typing import Optional

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from jinja2 import Environment, StrictUndefined, TemplateError
from pydantic import BaseModel, Field

from app.auth.access import require_admin
from app.auth.dependencies import _get_db
from src.repositories.setup_banner import SetupBannerRepository
from src.setup_banner import build_setup_banner_context


router = APIRouter(tags=["setup-banner"])

# Stub context used to validate that a saved template renders end-to-end,
# not just that it parses. Mirrors the shape of build_setup_banner_context() output.
_VALIDATION_STUB_CONTEXT = {
    "instance": {"name": "Example", "subtitle": "Example Org"},
    "server": {"url": "https://example.com", "hostname": "example.com"},
    "user": {"id": "u", "email": "user@example.com", "name": "User", "is_admin": False},
    "now": datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
    "today": "2026-01-01",
}


class BannerGetResponse(BaseModel):
    content: Optional[str]
    updated_at: Optional[str] = None
    updated_by: Optional[str] = None


class BannerPutRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=200_000)


class BannerPreviewRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=200_000)


class BannerPreviewResponse(BaseModel):
    content: str


@router.get("/api/admin/setup-banner", response_model=BannerGetResponse)
async def admin_get_banner(
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    row = SetupBannerRepository(conn).get()
    return BannerGetResponse(
        content=row["content"],
        updated_at=row["updated_at"].isoformat() if row["updated_at"] else None,
        updated_by=row["updated_by"],
    )


@router.put("/api/admin/setup-banner")
async def admin_put_banner(
    payload: BannerPutRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    env = Environment(undefined=StrictUndefined, autoescape=True)
    try:
        template = env.from_string(payload.content)
        # Render against a stub context so undefined placeholders or runtime
        # errors are caught here, not when an analyst visits /setup.
        template.render(**_VALIDATION_STUB_CONTEXT)
    except TemplateError as e:
        raise HTTPException(status_code=400, detail=f"Template invalid: {e}")
    SetupBannerRepository(conn).set(payload.content, updated_by=user["email"])
    return {"status": "ok"}


@router.delete("/api/admin/setup-banner", status_code=204)
async def admin_reset_banner(
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    SetupBannerRepository(conn).reset(updated_by=user["email"])
    return Response(status_code=204)


@router.post("/api/admin/setup-banner/preview", response_model=BannerPreviewResponse)
async def admin_preview_banner(
    payload: BannerPreviewRequest,
    request: Request,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Render arbitrary banner content against the live context for the
    calling admin, without persisting. Used by the /admin/setup-banner editor's
    Preview button so admins can see their edits before saving."""
    env = Environment(undefined=StrictUndefined, autoescape=True)
    try:
        template = env.from_string(payload.content)
        ctx = build_setup_banner_context(
            user=user,
            server_url=str(request.base_url).rstrip("/"),
        )
        rendered = template.render(**ctx)
    except TemplateError as e:
        raise HTTPException(status_code=400, detail=f"Template invalid: {e}")
    return BannerPreviewResponse(content=rendered)
