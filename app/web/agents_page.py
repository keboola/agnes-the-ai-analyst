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
from src.repositories import agent_memories_repo, agents_repo

router = APIRouter(tags=["web"])

# Agent PATs require all four scope modes to be 'selected' (see
# app/api/agents_admin.py::create_agent_token) — mirrored here so the page
# can render the "Issue token" control disabled instead of letting the user
# discover the 403 after clicking.
_SELECTED_MODE_FIELDS = ("plugins_mode", "connections_mode", "tables_mode", "memory_mode")


def _memories_for_panel(agent_id: str) -> list[dict]:
    """Every memory for `agent_id`, each carrying an `in_budget` flag for
    active rows (C4, agent-api V1c Task 5) — the exact same `select_in_budget`
    split `app.chat.agent_profile.materialize_memories` uses at spawn time,
    so the panel can never show a memory as "in effect" that a live spawn
    would actually shadow (or vice versa)."""
    from app.chat.agent_profile import _MEMORY_BUDGET_CHARS, select_in_budget

    repo = agent_memories_repo()
    memories = repo.list_for_agent(agent_id)
    active_rows = repo.list_active(agent_id)
    in_budget, _shadowed = select_in_budget(active_rows, _MEMORY_BUDGET_CHARS)
    in_budget_ids = {m["id"] for m in in_budget}
    return [{**m, "in_budget": (m["id"] in in_budget_ids) if m["status"] == "active" else None} for m in memories]


# Entry points: "My agents" in the user dropdown
# (`app/web/templates/_app_header.html`) plus a Cmd/Ctrl-K palette entry — a
# per-user resource list, so deliberately not primary nav and not the admin
# mega-menu (instance-level agent authoring is Studio's /admin/studio/agent).
# Both links are guarded by `tests/test_web_nav_agents.py`; don't drop them.
# Kept as a comment rather than a docstring: FastAPI copies docstrings into the
# OpenAPI description, and internal nav notes don't belong in the public schema.
@router.get("/agents", response_class=HTMLResponse)
async def agents_page(request: Request, user: dict = Depends(get_current_user)):
    rows = agents_repo().list_for_user(user["id"])
    agents = [
        {
            **row,
            "token_ready": all(row.get(field) == "selected" for field in _SELECTED_MODE_FIELDS),
            "memories": _memories_for_panel(row["id"]),
        }
        for row in rows
    ]
    ctx = _chrome_ctx(request, user)
    ctx["agents"] = agents
    return templates.TemplateResponse(request, "agents.html", ctx)
