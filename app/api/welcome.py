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
    # `"seed"` when the IWT clone owns this template (per-file detection
    # via src.initial_workspace.seed_owns) — the admin UI flips into
    # read-only mode and displays the seed's file content instead of the
    # local DB override. `"local"` otherwise (default editor experience).
    source: str = "local"
    # Path inside the seed repo that owns this template (only set when
    # source == "seed") so the admin UI can name the file in the banner.
    seed_path: Optional[str] = None


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
    # Per-file seed ownership: when the operator-configured IWT clone has
    # install-prompt/template.md.tmpl, the local DB override is dead-code
    # — the install-prompt renderer reads the seed file directly. Surface
    # the seed file content (read-only) so the admin sees what's actually
    # serving and isn't tricked by a working editor.
    from src.initial_workspace import resolve_seed_file, seed_owns

    server_url = str(request.base_url).rstrip("/")
    if seed_owns(_SEED_PATH):
        resolved = resolve_seed_file(_SEED_PATH)
        seed_content = resolved[0] if resolved is not None else ""
        live_default = compute_default_agent_prompt(conn, user=user, server_url=server_url)
        return TemplateGetResponse(
            content=seed_content,
            default=live_default,
            updated_at=None,
            updated_by=None,
            source="seed",
            seed_path=_SEED_PATH,
        )

    row = WelcomeTemplateRepository(conn).get()
    live_default = compute_default_agent_prompt(conn, user=user, server_url=server_url)
    return TemplateGetResponse(
        content=row["content"],
        default=live_default,
        updated_at=row["updated_at"].isoformat() if row["updated_at"] else None,
        updated_by=row["updated_by"],
        source="local",
    )


@router.put("/api/admin/welcome-template")
async def admin_put_template(
    payload: TemplatePutRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    # Refuse the save when the seed owns this template. Without this gate
    # the admin would silently write to a DB row the install-prompt
    # renderer no longer consults — a confusing "where did my edit go"
    # bug worse than a 409 with remediation text.
    from src.initial_workspace import seed_owns

    if seed_owns(_SEED_PATH):
        raise HTTPException(
            status_code=409,
            detail={
                "kind": "iwt_seed_owns_template",
                "seed_path": _SEED_PATH,
                "hint": (
                    "Initial Workspace Template owns this template. Edit "
                    f"`{_SEED_PATH}` in the seed repo + click 'Sync now' "
                    "in /admin/server-config."
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

    WelcomeTemplateRepository(conn).set(payload.content, updated_by=user["email"])
    return {"status": "ok"}


@router.delete("/api/admin/welcome-template", status_code=204)
async def admin_reset_template(
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    from src.initial_workspace import seed_owns

    if seed_owns(_SEED_PATH):
        raise HTTPException(
            status_code=409,
            detail={
                "kind": "iwt_seed_owns_template",
                "seed_path": _SEED_PATH,
                "hint": (
                    "Initial Workspace Template owns this template — "
                    "there is no local DB override to reset. Edit the "
                    f"seed repo's `{_SEED_PATH}` instead."
                ),
            },
        )
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
