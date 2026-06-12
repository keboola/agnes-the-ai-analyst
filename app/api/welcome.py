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
from src.welcome_template import build_context, compute_default_agent_prompt, render_agent_prompt_banner

from src.repositories import (
    welcome_template_repo,
)
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

# Same stub with user=None to validate templates against anonymous /setup visitors.
# /setup is publicly accessible — templates that reference user.* without an
# {% if user %} guard will crash with StrictUndefined for anon visitors.
_VALIDATION_STUB_CONTEXT_ANON = {
    **{k: v for k, v in _VALIDATION_STUB_CONTEXT.items() if k != "user"},
    "user": None,
}


class BannerResponse(BaseModel):
    content: str


class TemplateGetResponse(BaseModel):
    content: Optional[str]
    default: str  # live default from setup_instructions.resolve_lines()
    updated_at: Optional[str] = None
    updated_by: Optional[str] = None
    # #622: the prompt's source toggle. `"editor"` = DB override editable;
    # `"git"` = bound to the IWT clone file (editor read-only). `source` is
    # retained for backward-compat with the old grandfathered UI.
    source: str = "local"
    # Path inside the seed repo that owns this template (only set in git mode)
    # so the admin UI can name the file in the banner.
    seed_path: Optional[str] = None
    source_mode: str = "editor"
    git_path: Optional[str] = None


# Path inside the seed repo for the install-prompt template. Lives here
# as a constant so the admin UI's "edit in seed" banner and the GET/PUT/
# DELETE gate use the same value.
_SEED_PATH = "install-prompt/template.md.tmpl"


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
    # #622: the source toggle replaced the implicit seed_owns() read-only lock.
    # git mode surfaces the bound IWT file (read-only); editor mode keeps the
    # DB override editable even when an IWT repo is registered.
    server_url = str(request.base_url).rstrip("/")
    meta = welcome_template_repo().get_meta()
    live_default = compute_default_agent_prompt(conn, user=user, server_url=server_url)

    if meta["source_mode"] == "git":
        from src.initial_workspace import resolve_prompt

        git_content, _mode = resolve_prompt("install", conn)
        return TemplateGetResponse(
            content=git_content or "",
            default=live_default,
            updated_at=meta["updated_at"].isoformat() if meta["updated_at"] else None,
            updated_by=meta["updated_by"],
            source="seed",
            seed_path=meta["git_path"] or _SEED_PATH,
            source_mode="git",
            git_path=meta["git_path"] or _SEED_PATH,
        )

    return TemplateGetResponse(
        content=meta["content"],
        default=live_default,
        updated_at=meta["updated_at"].isoformat() if meta["updated_at"] else None,
        updated_by=meta["updated_by"],
        source="local",
        source_mode="editor",
    )


@router.put("/api/admin/welcome-template")
async def admin_put_template(
    payload: TemplatePutRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    # #622: the editor is always writable in editor mode. Saving is only
    # refused in git mode, where the DB content is dead-code the install-prompt
    # renderer would not consult (silent "where did my edit go" loss).
    if welcome_template_repo().get_meta()["source_mode"] == "git":
        raise HTTPException(
            status_code=409,
            detail={
                "kind": "prompt_in_git_mode",
                "hint": (
                    "This prompt is in Git source mode (bound to the Initial "
                    "Workspace Template repo). Switch to Editor override in "
                    "/admin/prompts before saving, or edit the bound file in "
                    "the repo + 'Sync now'."
                ),
            },
        )

    # Validate with autoescape=False to match every runtime render path
    # (/setup page, preview endpoint, render_agent_prompt_banner). The
    # outer template applies escaping where needed via `| e`. StrictUndefined
    # is kept so unknown placeholders are caught at save time.
    env = Environment(undefined=StrictUndefined, autoescape=False)
    try:
        template = env.from_string(payload.content)
        # Pass 1 — render with an authenticated user stub so undefined
        # placeholders or runtime errors are caught at save time.
        template.render(**_VALIDATION_STUB_CONTEXT)
    except TemplateError as e:
        raise HTTPException(status_code=400, detail=f"Template invalid: {e}")

    # Pass 2 — render with user=None to catch templates that reference user.*
    # fields without an {% if user %} guard.  /setup is publicly accessible to
    # anonymous visitors, so a guard-less template would crash with
    # StrictUndefined at runtime and silently fall back to the default — the
    # admin would have no idea their override is broken for anon visitors.
    try:
        template.render(**_VALIDATION_STUB_CONTEXT_ANON)
    except TemplateError as e:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Template fails for anonymous /setup visitors: {e}. "
                "Wrap user-dependent expressions in {{% if user %}}...{{% endif %}} — "
                "/setup is publicly accessible to non-logged-in users."
            ),
        )

    welcome_template_repo().set(payload.content, updated_by=user["email"])
    return {"status": "ok"}


@router.delete("/api/admin/welcome-template", status_code=204)
async def admin_reset_template(
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    # #622: reset allowed in editor mode; refused in git mode (no DB override
    # to clear — the renderer reads the bound repo file).
    if welcome_template_repo().get_meta()["source_mode"] == "git":
        raise HTTPException(
            status_code=409,
            detail={
                "kind": "prompt_in_git_mode",
                "hint": (
                    "This prompt is in Git source mode — there is no Editor "
                    "override to reset. Switch to Editor override in "
                    "/admin/prompts, or edit the bound repo file."
                ),
            },
        )
    welcome_template_repo().reset(updated_by=user["email"])
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
