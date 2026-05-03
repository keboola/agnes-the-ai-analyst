"""REST endpoints for the agent-workspace-prompt (analyst CLAUDE.md).

- GET  /api/welcome                              : analyst-facing rendered CLAUDE.md (auth required)
- GET  /api/admin/workspace-prompt-template      : raw template override + live default (admin)
- PUT  /api/admin/workspace-prompt-template      : set override (admin)
- DELETE /api/admin/workspace-prompt-template    : reset to default (admin)
- POST /api/admin/workspace-prompt-template/preview : live preview without persisting (admin)
"""

import datetime
import logging
from typing import Optional
from urllib.parse import unquote

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from jinja2 import Environment, StrictUndefined, TemplateError
from pydantic import BaseModel, Field

from app.auth.access import require_admin
from app.auth.dependencies import _get_db, get_current_user
from src.repositories.claude_md_template import ClaudeMdTemplateRepository
from src.claude_md import build_claude_md_context, compute_default_claude_md, render_claude_md

logger = logging.getLogger(__name__)


router = APIRouter(tags=["claude_md"])

# Stub context used to validate that a saved template renders end-to-end,
# not just that it parses. Mirrors the shape of build_claude_md_context() output.
# user is an authenticated user so templates that reference user.* are validated.
_VALIDATION_STUB_CONTEXT = {
    "instance": {"name": "Example", "subtitle": "Example Org"},
    "server": {"url": "https://example.com", "hostname": "example.com"},
    "sync_interval": "1h",
    "data_source": {"type": "keboola"},
    "tables": [{"name": "orders", "description": "Sample orders", "query_mode": "local"}],
    "metrics": {"count": 3, "categories": ["revenue", "growth"]},
    "marketplaces": [{"slug": "example", "name": "Example Marketplace", "plugins": [{"name": "plugin-a"}]}],
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

# Same stub with an anonymous-style user context to validate templates against
# the case where a user dict is present but minimal (analyst). The CLAUDE.md
# endpoint always requires auth, so user is never None — but templates may
# accidentally reference fields that aren't in the context.
_VALIDATION_STUB_CONTEXT_ANON = {
    **{k: v for k, v in _VALIDATION_STUB_CONTEXT.items() if k != "user"},
    "user": {
        "id": "u2",
        "email": "anon@example.com",
        "name": "",
        "is_admin": False,
        "groups": ["Everyone"],
    },
}


class ClaudeMdResponse(BaseModel):
    content: str


class TemplateGetResponse(BaseModel):
    content: Optional[str]
    default: str  # live default rendered with calling admin's context
    updated_at: Optional[str] = None
    updated_by: Optional[str] = None


class TemplatePutRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=200_000)


class TemplatePreviewRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=200_000)


# ---------------------------------------------------------------------------
# Analyst-facing endpoint — returns rendered CLAUDE.md
# ---------------------------------------------------------------------------

@router.get("/api/welcome", response_model=ClaudeMdResponse)
async def get_welcome(
    request: Request,
    server_url: Optional[str] = Query(None, description="Server URL used in rendered CLAUDE.md"),
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Return the rendered CLAUDE.md for the authenticated analyst.

    The CLI calls this endpoint during ``da analyst setup`` to write
    ``<workspace>/CLAUDE.md``. The content is RBAC-filtered per the
    calling user.

    ``server_url`` query param lets the CLI pass the origin it knows so
    the rendered content references the correct server URL rather than the
    request host (which may differ behind a proxy).
    """
    effective_url = server_url or str(request.base_url).rstrip("/")
    try:
        content = render_claude_md(conn, user=user, server_url=effective_url)
    except TemplateError as exc:
        logger.warning("render_claude_md failed (template error): %s", exc)
        raise HTTPException(status_code=500, detail=f"Template render error: {exc}")
    except Exception:
        logger.exception("render_claude_md failed (unexpected)")
        raise HTTPException(status_code=500, detail="Internal error rendering CLAUDE.md")
    return ClaudeMdResponse(content=content)


# ---------------------------------------------------------------------------
# Admin endpoints — CRUD for the workspace-prompt template override
# ---------------------------------------------------------------------------

@router.get("/api/admin/workspace-prompt-template", response_model=TemplateGetResponse)
async def admin_get_workspace_template(
    request: Request,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    row = ClaudeMdTemplateRepository(conn).get()
    server_url = str(request.base_url).rstrip("/")
    live_default = compute_default_claude_md(conn, user=user, server_url=server_url)
    return TemplateGetResponse(
        content=row["content"],
        default=live_default,
        updated_at=row["updated_at"].isoformat() if row["updated_at"] else None,
        updated_by=row["updated_by"],
    )


@router.put("/api/admin/workspace-prompt-template")
async def admin_put_workspace_template(
    payload: TemplatePutRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Save an admin override for the analyst CLAUDE.md template.

    Two-pass Jinja2 validation (autoescape=False, StrictUndefined):
    - Pass 1: render with an authenticated user stub — catches undefined
      placeholders and syntax errors.
    - Pass 2: render with a minimal anon-style user stub — catches templates
      that hard-depend on admin-only context fields.
    """
    env = Environment(undefined=StrictUndefined, autoescape=False)
    try:
        template = env.from_string(payload.content)
        template.render(**_VALIDATION_STUB_CONTEXT)
    except TemplateError as e:
        raise HTTPException(status_code=400, detail=f"Template invalid: {e}")

    try:
        template.render(**_VALIDATION_STUB_CONTEXT_ANON)
    except TemplateError as e:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Template fails for non-admin analyst users: {e}. "
                "Wrap user-dependent expressions in {{% if user.is_admin %}}...{{% endif %}} "
                "or ensure the template renders correctly for all users."
            ),
        )

    ClaudeMdTemplateRepository(conn).set(payload.content, updated_by=user["email"])
    return {"status": "ok"}


@router.delete("/api/admin/workspace-prompt-template", status_code=204)
async def admin_reset_workspace_template(
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    ClaudeMdTemplateRepository(conn).reset(updated_by=user["email"])
    return Response(status_code=204)


@router.post("/api/admin/workspace-prompt-template/preview", response_model=ClaudeMdResponse)
async def admin_preview_workspace_template(
    payload: TemplatePreviewRequest,
    request: Request,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Render arbitrary template content against the live RBAC context for the
    calling admin, without persisting. Used by the /admin/workspace-prompt editor's
    Preview button so admins can see their edits before saving."""
    env = Environment(undefined=StrictUndefined, autoescape=False)
    try:
        template = env.from_string(payload.content)
        ctx = build_claude_md_context(
            conn, user=user, server_url=str(request.base_url).rstrip("/")
        )
        rendered = template.render(**ctx)
    except TemplateError as e:
        raise HTTPException(status_code=400, detail=f"Template invalid: {e}")
    return ClaudeMdResponse(content=rendered)
