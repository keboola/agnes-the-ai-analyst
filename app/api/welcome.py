"""REST endpoints for the agent-setup-prompt.

- GET  /api/admin/welcome-template   : raw template override + live default (admin)
- PUT  /api/admin/welcome-template   : set override (admin)
- DELETE /api/admin/welcome-template : reset to default (admin)
- POST /api/admin/welcome-template/preview : live preview without persisting (admin)
"""

import datetime
import logging
from typing import Optional

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from jinja2 import Environment, StrictUndefined, TemplateError
from pydantic import BaseModel, Field

from app.auth.access import require_admin
from app.auth.dependencies import _get_db
from src.repositories.welcome_template import WelcomeTemplateRepository
from src.welcome_template import build_context, compute_default_agent_prompt, render_agent_prompt_banner

logger = logging.getLogger(__name__)


router = APIRouter(tags=["welcome"])

# Stub context used to validate that a saved template renders end-to-end,
# not just that it parses. Mirrors the shape of build_context() output.
# user may be None for anonymous visitors; the stub uses an authenticated
# user so templates that reference user.* fields are validated.
_VALIDATION_STUB_CONTEXT = {
    "instance": {"name": "Example", "subtitle": "Example Org"},
    "server": {"url": "https://example.com", "hostname": "example.com"},
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


class BannerResponse(BaseModel):
    content: str


class TemplateGetResponse(BaseModel):
    content: Optional[str]
    default: str  # live default from setup_instructions.resolve_lines()
    updated_at: Optional[str] = None
    updated_by: Optional[str] = None


class TemplatePutRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=200_000)


class TemplatePreviewRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=200_000)


@router.get("/api/admin/welcome-template", response_model=TemplateGetResponse)
async def admin_get_template(
    request: Request,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    row = WelcomeTemplateRepository(conn).get()
    server_url = str(request.base_url).rstrip("/")
    live_default = compute_default_agent_prompt(conn, user=user, server_url=server_url)
    return TemplateGetResponse(
        content=row["content"],
        default=live_default,
        updated_at=row["updated_at"].isoformat() if row["updated_at"] else None,
        updated_by=row["updated_by"],
    )


@router.put("/api/admin/welcome-template")
async def admin_put_template(
    payload: TemplatePutRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    # Validate with autoescape=False to match every runtime render path
    # (/setup page, preview endpoint, render_agent_prompt_banner). The
    # outer template applies escaping where needed via `| e`. StrictUndefined
    # is kept so unknown placeholders are caught at save time.
    env = Environment(undefined=StrictUndefined, autoescape=False)
    try:
        template = env.from_string(payload.content)
        # Render against a stub context so undefined placeholders or runtime
        # errors are caught here, not when /setup renders for a real user.
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


@router.post("/api/admin/welcome-template/preview", response_model=BannerResponse)
async def admin_preview_template(
    payload: TemplatePreviewRequest,
    request: Request,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Render arbitrary template content against the live context for the
    calling admin, without persisting. Used by the /admin/agent-prompt editor's
    Preview button so admins can see their edits before saving."""
    # autoescape=False to match /setup rendering — the outer Jinja2 template
    # applies escaping where needed.
    env = Environment(undefined=StrictUndefined, autoescape=False)
    try:
        template = env.from_string(payload.content)
        ctx = build_context(
            user=user, server_url=str(request.base_url).rstrip("/")
        )
        rendered = template.render(**ctx)
    except TemplateError as e:
        raise HTTPException(status_code=400, detail=f"Template invalid: {e}")
    return BannerResponse(content=rendered)
