"""REST endpoints for the analyst-onboarding welcome prompt.

- GET  /api/welcome                  : render for the calling user (auth required)
- GET  /api/admin/welcome-template   : raw template + shipped default (admin)
- PUT  /api/admin/welcome-template   : set override (admin)
- DELETE /api/admin/welcome-template : reset to default (admin)
"""

import datetime
import logging
from typing import Optional

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from jinja2 import Environment, StrictUndefined, TemplateError, TemplateSyntaxError
from pydantic import BaseModel, Field

from app.auth.access import require_admin
from app.auth.dependencies import _get_db, get_current_user
from src.repositories.welcome_template import WelcomeTemplateRepository
from src.welcome_template import _load_default_template, build_context, render_welcome

logger = logging.getLogger(__name__)


router = APIRouter(tags=["welcome"])

# Stub context used to validate that a saved template renders end-to-end,
# not just that it parses. Mirrors the shape of build_context() output.
_VALIDATION_STUB_CONTEXT = {
    "instance": {"name": "Example", "subtitle": "Example Org"},
    "server": {"url": "https://example.com", "hostname": "example.com"},
    "sync_interval": "1 hour",
    "data_source": {"type": "local"},
    "tables": [{"name": "example", "description": "", "query_mode": "local"}],
    "metrics": {"count": 0, "categories": []},
    "marketplaces": [
        {"slug": "example", "name": "Example Marketplace", "plugins": [{"name": "x"}]}
    ],
    "user": {
        "id": "u",
        "email": "user@example.com",
        "name": "User",
        "is_admin": False,
        "groups": ["Everyone"],
    },
    "now": datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
    "today": "2026-01-01",
}


class WelcomeResponse(BaseModel):
    content: str


class TemplateGetResponse(BaseModel):
    content: Optional[str]
    default: str
    updated_at: Optional[str] = None
    updated_by: Optional[str] = None


class TemplatePutRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=200_000)


class TemplatePreviewRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=200_000)


@router.get("/api/welcome", response_model=WelcomeResponse)
async def get_welcome(
    server_url: str = Query(..., description="The server URL the analyst is bootstrapping against"),
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Render the welcome prompt for the calling user. Returns rendered markdown."""
    try:
        rendered = render_welcome(conn, user=user, server_url=server_url)
    except TemplateError as e:
        logger.warning("Welcome render failed: %s", e, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Welcome template render failed. An admin can fix it at /admin/welcome.",
        )
    return WelcomeResponse(content=rendered)


@router.get("/api/admin/welcome-template", response_model=TemplateGetResponse)
async def admin_get_template(
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    row = WelcomeTemplateRepository(conn).get()
    return TemplateGetResponse(
        content=row["content"],
        default=_load_default_template(),
        updated_at=row["updated_at"].isoformat() if row["updated_at"] else None,
        updated_by=row["updated_by"],
    )


@router.put("/api/admin/welcome-template")
async def admin_put_template(
    payload: TemplatePutRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    env = Environment(undefined=StrictUndefined)
    try:
        template = env.from_string(payload.content)
        # Render against a stub context so undefined placeholders or runtime
        # errors are caught here, not when an analyst calls /api/welcome.
        template.render(**_VALIDATION_STUB_CONTEXT)
    except TemplateError as e:
        raise HTTPException(status_code=400, detail=f"Template invalid: {e}")
    WelcomeTemplateRepository(conn).set(payload.content, updated_by=user["email"])
    return {"status": "ok"}


@router.delete("/api/admin/welcome-template", status_code=204)
async def admin_reset_template(
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    WelcomeTemplateRepository(conn).reset(updated_by=user["email"])
    return Response(status_code=204)


@router.post("/api/admin/welcome-template/preview", response_model=WelcomeResponse)
async def admin_preview_template(
    payload: TemplatePreviewRequest,
    request: Request,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Render arbitrary template content against the live context for the
    calling admin, without persisting. Used by the /admin/welcome editor's
    Preview button so admins can see their edits before saving."""
    env = Environment(undefined=StrictUndefined, autoescape=False)
    try:
        template = env.from_string(payload.content)
        ctx = build_context(conn, user=user, server_url=str(request.base_url).rstrip("/"))
        rendered = template.render(**ctx)
    except TemplateError as e:
        raise HTTPException(status_code=400, detail=f"Template invalid: {e}")
    return WelcomeResponse(content=rendered)
