"""REST endpoints for the analyst-onboarding welcome prompt.

- GET  /api/welcome                  : render for the calling user (auth required)
- GET  /api/admin/welcome-template   : raw template + shipped default (admin)
- PUT  /api/admin/welcome-template   : set override (admin)
- DELETE /api/admin/welcome-template : reset to default (admin)
"""

from typing import Optional

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from jinja2 import TemplateSyntaxError
from pydantic import BaseModel, Field

from app.auth.access import require_admin
from app.auth.dependencies import _get_db, get_current_user
from src.repositories.welcome_template import WelcomeTemplateRepository
from src.welcome_template import _load_default_template, render_welcome


router = APIRouter(tags=["welcome"])


class WelcomeResponse(BaseModel):
    content: str


class TemplateGetResponse(BaseModel):
    content: Optional[str]
    default: str
    updated_at: Optional[str] = None
    updated_by: Optional[str] = None


class TemplatePutRequest(BaseModel):
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
    except TemplateSyntaxError as e:
        raise HTTPException(
            status_code=500,
            detail=f"Welcome template has a syntax error: {e.message}. Reset via /admin/welcome.",
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
    from jinja2 import Environment, StrictUndefined
    try:
        Environment(undefined=StrictUndefined).parse(payload.content)
    except TemplateSyntaxError as e:
        raise HTTPException(status_code=400, detail=f"Jinja2 syntax error: {e.message}")
    WelcomeTemplateRepository(conn).set(payload.content, updated_by=user["email"])
    return {"status": "ok"}


@router.delete("/api/admin/welcome-template", status_code=204)
async def admin_reset_template(
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    WelcomeTemplateRepository(conn).reset(updated_by=user["email"])
    return Response(status_code=204)
