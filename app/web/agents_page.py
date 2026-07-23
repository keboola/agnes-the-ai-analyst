"""`GET /agents` — minimal builder UI over the agent management API (Task 10).

Server-renders the caller's own agent profiles (`agents_repo().list_for_user`);
the create form and the per-agent "Issue token" / "Delete" actions are driven
client-side (fetch) against the management API in `app/api/agents_admin.py`
(`/api/v1/agents`) — this module only renders the list + shell.

A standalone `APIRouter` (mirrors `app/api/agents_admin.py`'s own-module
pattern) rather than growing the monolithic `app/web/router.py` further. Must
be included in `app/main.py` *before* `app.include_router(web_router)` — that
router's last route is a `/{full_path:path}` catch-all that would otherwise
shadow `/agents`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.auth.dependencies import get_current_user
from app.web.router import _chrome_ctx, templates
from src.repositories import agents_repo

router = APIRouter(tags=["web"])

# Agent PATs require all four scope modes to be 'selected' (see
# app/api/agents_admin.py::create_agent_token) — mirrored here so the page
# can render the "Issue token" control disabled instead of letting the user
# discover the 403 after clicking.
_SELECTED_MODE_FIELDS = ("plugins_mode", "connections_mode", "tables_mode", "memory_mode")


@router.get("/agents", response_class=HTMLResponse)
async def agents_page(request: Request, user: dict = Depends(get_current_user)):
    rows = agents_repo().list_for_user(user["id"])
    agents = [
        {**row, "token_ready": all(row.get(field) == "selected" for field in _SELECTED_MODE_FIELDS)} for row in rows
    ]
    ctx = _chrome_ctx(request, user)
    ctx["agents"] = agents
    return templates.TemplateResponse(request, "agents.html", ctx)
