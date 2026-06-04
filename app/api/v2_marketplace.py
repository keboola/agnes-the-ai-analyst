"""Lightweight v2 marketplace endpoint for MCP and programmatic consumers.

Intentionally minimal: no telemetry, no enrichment, no pagination — just the
skill content a Claude Code agent needs to load skills into its context.

Endpoint:
    GET /api/v2/marketplace/skills

Returns every SKILL.md the caller is RBAC-authorised to read, with the
frontmatter stripped from the body so the plain instruction text lands in
the MCP response. One call, flat list — no follow-up fetches needed.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.auth.access import _user_group_ids, is_user_admin
from app.auth.dependencies import get_current_user
from app.utils import get_marketplaces_dir
from src.marketplace_listing import _FRONTMATTER_RE, _parse_frontmatter
from src.repositories import marketplace_plugins_repo

router = APIRouter(prefix="/api/v2/marketplace", tags=["marketplace-v2"])


class SkillEntry(BaseModel):
    marketplace_id: str
    plugin_name: str
    skill_name: str
    name: str
    description: Optional[str] = None
    invocation: Optional[str] = None
    body: str


class SkillsResponse(BaseModel):
    skills: List[SkillEntry]


def _body(text: str) -> str:
    m = _FRONTMATTER_RE.match(text)
    return text[m.end():].lstrip("\n") if m else text


def _skills_for_plugin(
    marketplace_id: str,
    plugin_name: str,
) -> List[SkillEntry]:
    plugin_root = Path(get_marketplaces_dir()) / marketplace_id / "plugins" / plugin_name
    skills_dir = plugin_root / "skills"
    if not skills_dir.is_dir():
        return []
    out: List[SkillEntry] = []
    for skill_dir in sorted(skills_dir.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file():
            continue
        try:
            text = skill_md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fm = _parse_frontmatter(text)
        out.append(SkillEntry(
            marketplace_id=marketplace_id,
            plugin_name=plugin_name,
            skill_name=skill_dir.name,
            name=fm.get("name") or skill_dir.name,
            description=fm.get("description"),
            invocation=fm.get("invocation"),
            body=_body(text),
        ))
    return out


def _accessible_plugins(user: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return all marketplace_plugins rows the caller can access.

    Backend-aware throughout: the plugin read goes through
    ``marketplace_plugins_repo()`` and the RBAC checks call ``is_user_admin`` /
    ``_user_group_ids`` WITHOUT a connection, so they fall back to the factory.
    Passing a raw DuckDB conn here read an empty plugin set + wrong-backend
    group membership on a Postgres instance.
    """
    if is_user_admin(user["id"]):
        return marketplace_plugins_repo().list_all()
    group_ids = _user_group_ids(user["id"]) or set()
    items, _ = marketplace_plugins_repo().list_with_filters(
        group_ids=group_ids,
        limit=10_000,
    )
    return items


@router.get("/skills", response_model=SkillsResponse)
async def list_skills(
    user: dict = Depends(get_current_user),
):
    """Return all skills from accessible marketplace plugins.

    RBAC-filtered: admins see everything; regular users see only plugins
    their groups have ``resource_grants`` for. Each entry includes the full
    SKILL.md body (frontmatter stripped) so MCP clients can load it directly
    into Claude's context without a follow-up request.
    """
    plugins = _accessible_plugins(user)
    skills: List[SkillEntry] = []
    for plugin in plugins:
        skills.extend(
            _skills_for_plugin(plugin["marketplace_id"], plugin["name"])
        )
    return SkillsResponse(skills=skills)
