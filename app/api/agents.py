"""Agents API — CRUD for the caller's composed assistants (v103).

Endpoints:

  GET    /api/agents                 auth (owned ∪ shared-with-my-groups)
  POST   /api/agents                 auth (owned by creator)
  GET    /api/agents/{agent_id}      owner, grantee, or admin
  PATCH  /api/agents/{agent_id}      owner or admin
  DELETE /api/agents/{agent_id}      owner or admin

RBAC model: **create** = any authenticated user (the agent is owned by its
creator and private until shared); **read** = owner, admin, or any user whose
groups hold a ``resource_grants`` row for ``(agent, <agent_id>)``;
**update/delete** = owner or admin. Sharing itself is written through
``/api/sharing`` (``app/api/sharing.py``), so a grant made there is honored
here — the agent read path is the reader that makes agent grants real rather
than decorative.

Fail-closed: an agent the caller cannot read returns 404 (not 403), matching
the collections contract, so callers cannot probe for existence.

Before v103 agent definitions lived only in the browser's ``localStorage``, so
they could not be listed on a second device, surfaced in the Library, or
shared. This API is the registry that replaced that.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth.access import is_user_admin
from app.auth.dependencies import get_current_user
from app.resource_types import ResourceType
from src.repositories import agents_repo, resource_grants_repo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agents", tags=["agents"])

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_RT = ResourceType.AGENT.value


def _auto_slug(name: str) -> str:
    """URL-safe slug from an agent name.

    Falls back to ``"agent"`` for names with no alphanumerics (e.g. "!!!"),
    which would otherwise collapse to an empty slug and cause spurious
    collisions on the second such name. Mirrors
    ``app/api/collections.py::_auto_slug``.
    """
    return _SLUG_RE.sub("-", name.lower()).strip("-")[:100].strip("-") or "agent"


def _unique_slug(base: str) -> str:
    """First free slug in ``base``, ``base-2``, ``base-3``, …

    Agents are user-named and duplicates are ordinary (two people, or one
    person twice, naming an agent "Analyst"), so a name clash must not surface
    as a 409 the way an admin-curated slug would.

    ``include_deleted=True`` is load-bearing: ``delete`` only sets
    ``deleted_at``, while the ``slug`` UNIQUE constraint spans deleted rows on
    both backends. Searching live rows only would report a soft-deleted agent's
    slug as free and drive the INSERT straight into a ConstraintException — a
    500 on the ordinary create-delete-create path.
    """
    repo = agents_repo()
    if repo.get_by_slug(base, include_deleted=True) is None:
        return base
    for n in range(2, 1000):
        candidate = f"{base}-{n}"[:100].strip("-")
        if repo.get_by_slug(candidate, include_deleted=True) is None:
            return candidate
    # Pathological (999 same-named agents) — fall back to a random suffix
    # rather than raising, so creation never hard-fails on naming alone.
    import secrets

    return f"{base}-{secrets.token_hex(4)}"[:100]


class AgentCreate(BaseModel):
    name: str = Field(default="", max_length=200)
    role: str = Field(default="", max_length=500)
    instructions: str = ""
    tone: str = Field(default="concise", max_length=50)
    greeting: str = ""
    knowledge: List[str] = Field(default_factory=list)
    plugins: List[str] = Field(default_factory=list)
    surfaces: Optional[Dict[str, bool]] = None
    status: str = Field(default="draft", max_length=30)


class AgentUpdate(BaseModel):
    """All fields optional — a PATCH sends only what changed."""

    name: Optional[str] = Field(default=None, max_length=200)
    role: Optional[str] = Field(default=None, max_length=500)
    instructions: Optional[str] = None
    tone: Optional[str] = Field(default=None, max_length=50)
    greeting: Optional[str] = None
    knowledge: Optional[List[str]] = None
    plugins: Optional[List[str]] = None
    surfaces: Optional[Dict[str, bool]] = None
    status: Optional[str] = Field(default=None, max_length=30)


def _granted_agent_ids(user_id: str) -> set:
    """Agent ids granted to one of ``user_id``'s groups."""
    try:
        return set(resource_grants_repo().list_resource_ids_for_user(user_id, _RT))
    except Exception as e:
        logger.warning("agents: could not resolve grants for %s: %s", user_id, e)
        return set()


def _agent_out(row: dict, *, uid: str) -> Dict[str, Any]:
    """Wire shape. Mirrors the builder's in-browser object 1:1 (the builder
    was written against this shape while it was still localStorage-only), plus
    server-side ownership so the Library can label rows without a second call.
    """
    owned = row.get("created_by") == uid
    return {
        "id": row["id"],
        "slug": row.get("slug"),
        "name": row.get("name") or "",
        "role": row.get("role") or "",
        "instructions": row.get("instructions") or "",
        "tone": row.get("tone") or "concise",
        "greeting": row.get("greeting") or "",
        "knowledge": row.get("knowledge") or [],
        "plugins": row.get("plugins") or [],
        "surfaces": row.get("surfaces") or {},
        "status": row.get("status") or "draft",
        "mine": owned,
        "created_by": row.get("created_by"),
        "created_at": row["created_at"].isoformat() if row.get("created_at") is not None else None,
        "updated": row["updated_at"].isoformat() if row.get("updated_at") is not None else None,
    }


def _readable(agent_id: str, user: dict) -> dict:
    """Fetch an agent the caller may READ, else 404.

    Readable = owner, admin, or a grantee via one of their groups.
    """
    row = agents_repo().get(agent_id)
    if not row:
        raise HTTPException(status_code=404, detail="agent_not_found")
    uid = user["id"]
    if row.get("created_by") == uid:
        return row
    if is_user_admin(uid):
        return row
    if agent_id in _granted_agent_ids(uid):
        return row
    raise HTTPException(status_code=404, detail="agent_not_found")


def _writable(agent_id: str, user: dict) -> dict:
    """Fetch an agent the caller may MUTATE, else 404.

    A grant conveys *use*, not authorship — only the owner (or an admin) may
    edit or delete, so a shared agent can't be rewritten under its author.
    """
    row = agents_repo().get(agent_id)
    if not row:
        raise HTTPException(status_code=404, detail="agent_not_found")
    if row.get("created_by") != user["id"] and not is_user_admin(user["id"]):
        raise HTTPException(status_code=404, detail="agent_not_found")
    return row


@router.get("")
async def list_agents(user: dict = Depends(get_current_user)):
    """The caller's agents plus any shared into a group they belong to.

    Deliberately NOT admin god-mode: an admin sees their own agent list here,
    not every agent in the instance (that audit view is /admin/access).
    """
    uid = user["id"]
    granted = _granted_agent_ids(uid)
    out: List[Dict[str, Any]] = []
    try:
        for row in agents_repo().list():
            if row.get("created_by") == uid or row["id"] in granted:
                out.append(_agent_out(row, uid=uid))
    except Exception as e:
        logger.warning("agents: could not enumerate: %s", e)
    return {"agents": out}


@router.post("", status_code=201)
async def create_agent(payload: AgentCreate, user: dict = Depends(get_current_user)):
    """Create an agent owned by (and private to) the caller.

    An unnamed agent is legal — the builder creates the row first and the user
    names it as they go, so a blank name must not 422 the whole flow.
    """
    name = (payload.name or "").strip()
    slug = _unique_slug(_auto_slug(name or "agent"))
    agent_id = agents_repo().create(
        name=name,
        slug=slug,
        created_by=user["id"],
        role=payload.role or "",
        instructions=payload.instructions or "",
        tone=payload.tone or "concise",
        greeting=payload.greeting or "",
        knowledge=payload.knowledge,
        plugins=payload.plugins,
        surfaces=payload.surfaces,
        status=payload.status or "draft",
    )
    logger.info("agent created id=%s slug=%s by=%s", agent_id, slug, user.get("email"))
    row = agents_repo().get(agent_id)
    return _agent_out(row or {}, uid=user["id"])


@router.get("/{agent_id}")
async def get_agent(agent_id: str, user: dict = Depends(get_current_user)):
    """One agent — owner, grantee, or admin."""
    return _agent_out(_readable(agent_id, user), uid=user["id"])


@router.patch("/{agent_id}")
async def update_agent(
    agent_id: str,
    payload: AgentUpdate,
    user: dict = Depends(get_current_user),
):
    """Patch an agent the caller owns. Only supplied fields change."""
    _writable(agent_id, user)
    fields = payload.model_dump(exclude_unset=True, exclude_none=True)
    if fields:
        agents_repo().update(agent_id, **fields)
    row = agents_repo().get(agent_id)
    if not row:
        raise HTTPException(status_code=404, detail="agent_not_found")
    return _agent_out(row, uid=user["id"])


@router.delete("/{agent_id}", status_code=204)
async def delete_agent(agent_id: str, user: dict = Depends(get_current_user)):
    """Soft-delete an agent the caller owns, and drop its grants.

    Grants are removed too, so a later agent can never inherit a dangling
    grant through id reuse and /admin/access shows no orphan rows.
    """
    _writable(agent_id, user)
    agents_repo().soft_delete(agent_id)
    try:
        resource_grants_repo().delete_by_resource(_RT, agent_id)
    except Exception as e:
        logger.warning("agents: grant cleanup failed for %s: %s", agent_id, e)
    logger.info("agent deleted id=%s by=%s", agent_id, user.get("email"))
    return None
