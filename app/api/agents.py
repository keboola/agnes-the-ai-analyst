"""Agents API — CRUD for the caller's composed assistants.

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

This is the paper-theme agent-BUILDER surface. When the paper-theme branch
merged into main it stopped owning its own ``agents`` table: main's
agent-as-API subsystem is the canonical owner, and the builder's authored
fields ride it as a SUPERSET (``src/db.py`` v110 / ``src/models/agents.py``).
This module is the thin adapter between the builder's wire shape and
``AgentsRepository`` — it maps the builder's ``created_by`` → the table's
``owner_user_id`` and ``instructions`` → ``system_prompt``, and JSON-encodes
the opaque ``knowledge`` / ``plugins`` / ``surfaces`` id-lists into the
TEXT columns.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
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
# Reserved for the per-owner seeded default agent (``agents_repo().
# get_or_create_default``), never claimable by a user-created one. Mirrors
# ``app/api/agents_admin.py``'s ``_RESERVED_SLUGS``.
_DEFAULT_AGENT_SLUG = "default"


def _auto_slug(name: str) -> str:
    """URL-safe slug from an agent name.

    Falls back to ``"agent"`` for names with no alphanumerics (e.g. "!!!"),
    which would otherwise collapse to an empty slug and cause spurious
    collisions on the second such name. Mirrors
    ``app/api/collections.py::_auto_slug``.
    """
    return _SLUG_RE.sub("-", name.lower()).strip("-")[:100].strip("-") or "agent"


def _unique_slug(base: str, owner_user_id: str) -> str:
    """First free slug in ``base``, ``base-2``, ``base-3``, … for one owner.

    Agents are user-named and duplicates are ordinary (one person naming two
    agents "Analyst"), so a name clash must not surface as a 409 the way an
    admin-curated slug would. The table's ``(owner_user_id, slug)`` UNIQUE is
    per-owner, so the search is scoped to the owner.

    ``include_deleted=True`` is load-bearing: ``delete`` only sets
    ``deleted_at`` while the UNIQUE spans deleted rows, so searching live rows
    only would report a soft-deleted agent's slug as free and drive the INSERT
    straight into a ConstraintException.

    ``"default"`` is reserved for the per-owner seeded default agent, exactly as
    ``app/api/agents_admin.py``'s ``_RESERVED_SLUGS`` treats it. The governance
    router rejects it outright; here the slug is derived from a user-typed name,
    so an agent called "Default" is suffixed to ``default-2`` rather than 400'd.
    Without this an ordinary name could claim the slug before the owner's first
    chat seeded the real default — which then lands on ``default-2``, and
    ``POST /api/v1/agents/default/responses`` would address the user's agent
    instead of the default.
    """
    repo = agents_repo()
    if base != _DEFAULT_AGENT_SLUG and repo.get_by_slug(owner_user_id, base, include_deleted=True) is None:
        return base
    for n in range(2, 1000):
        candidate = f"{base}-{n}"[:100].strip("-")
        if repo.get_by_slug(owner_user_id, candidate, include_deleted=True) is None:
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


def _decode(raw: Any, fallback: Any) -> Any:
    """Decode a JSON-text column (knowledge/plugins/surfaces) to its object."""
    if raw is None or raw == "":
        return fallback
    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return fallback


def _granted_agent_ids(user_id: str) -> set:
    """Agent ids granted to one of ``user_id``'s groups."""
    try:
        return set(resource_grants_repo().list_resource_ids_for_user(user_id, _RT))
    except Exception as e:
        logger.warning("agents: could not resolve grants for %s: %s", user_id, e)
        return set()


def _agent_out(row: dict, *, uid: str) -> Dict[str, Any]:
    """Wire shape — the builder's in-browser object 1:1, projected off main's
    canonical row (``owner_user_id`` → ``created_by``, ``system_prompt`` →
    ``instructions``, JSON-text id-lists decoded), plus server-side ownership
    so the Library can label rows without a second call.
    """
    owner = row.get("owner_user_id")
    return {
        "id": row["id"],
        "slug": row.get("slug"),
        "name": row.get("name") or "",
        "role": row.get("role") or "",
        "instructions": row.get("system_prompt") or "",
        "tone": row.get("tone") or "concise",
        "greeting": row.get("greeting") or "",
        "knowledge": _decode(row.get("knowledge"), []),
        "plugins": _decode(row.get("plugins"), []),
        "surfaces": _decode(row.get("surfaces"), {}),
        "status": row.get("status") or "draft",
        "mine": owner == uid,
        "created_by": owner,
        "created_at": row["created_at"].isoformat() if row.get("created_at") is not None else None,
        "updated": row["updated_at"].isoformat() if row.get("updated_at") is not None else None,
    }


def _live(agent_id: str) -> Optional[dict]:
    """Fetch a non-soft-deleted agent by id (``get_by_id`` includes tombstones)."""
    row = agents_repo().get_by_id(agent_id)
    if not row or row.get("deleted_at") is not None:
        return None
    return row


def _readable(agent_id: str, user: dict) -> dict:
    """Fetch an agent the caller may READ, else 404.

    Readable = owner, admin, or a grantee via one of their groups.
    """
    row = _live(agent_id)
    if not row:
        raise HTTPException(status_code=404, detail="agent_not_found")
    uid = user["id"]
    if row.get("owner_user_id") == uid:
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
    row = _live(agent_id)
    if not row:
        raise HTTPException(status_code=404, detail="agent_not_found")
    if row.get("owner_user_id") != user["id"] and not is_user_admin(user["id"]):
        raise HTTPException(status_code=404, detail="agent_not_found")
    return row


@router.get("")
async def list_agents(user: dict = Depends(get_current_user)):
    """The caller's agents plus any shared into a group they belong to.

    Deliberately NOT admin god-mode: an admin sees their own agent list here,
    not every agent in the instance (that audit view is /admin/access).
    """
    uid = user["id"]
    repo = agents_repo()
    out: List[Dict[str, Any]] = []
    seen: set = set()
    try:
        for row in repo.list_for_user(uid):
            out.append(_agent_out(row, uid=uid))
            seen.add(row["id"])
    except Exception as e:
        logger.warning("agents: could not enumerate for %s: %s", uid, e)
    for agent_id in _granted_agent_ids(uid):
        if agent_id in seen:
            continue
        row = _live(agent_id)
        if row:
            out.append(_agent_out(row, uid=uid))
    return {"agents": out}


@router.post("", status_code=201)
async def create_agent(payload: AgentCreate, user: dict = Depends(get_current_user)):
    """Create an agent owned by (and private to) the caller.

    An unnamed agent is legal — the builder creates the row first and the user
    names it as they go, so a blank name must not 422 the whole flow.
    """
    uid = user["id"]
    name = (payload.name or "").strip()
    slug = _unique_slug(_auto_slug(name or "agent"), uid)
    # Builder ids carry the ``agt_`` prefix (the redesign's convention, asserted
    # by the Library sharing tests) so a builder-authored agent is
    # distinguishable at a glance from the agent-as-API rows that main's
    # get_or_create_default seeds with a bare UUID.
    agent_id = "agt_" + uuid.uuid4().hex
    agents_repo().create(
        id=agent_id,
        owner_user_id=uid,
        name=name,
        slug=slug,
        system_prompt=payload.instructions or "",
        role=payload.role or "",
        tone=payload.tone or "concise",
        greeting=payload.greeting or "",
        knowledge=json.dumps(payload.knowledge or []),
        plugins=json.dumps(payload.plugins or []),
        # A new agent is web-enabled by default (the builder's convention); an
        # explicit surfaces payload overrides it.
        surfaces=json.dumps(payload.surfaces if payload.surfaces is not None else {"web": True}),
        status=payload.status or "draft",
    )
    logger.info("agent created id=%s slug=%s by=%s", agent_id, slug, user.get("email"))
    row = _live(agent_id)
    return _agent_out(row or {}, uid=uid)


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
    supplied = payload.model_dump(exclude_unset=True, exclude_none=True)
    # Map the builder's wire names onto the canonical columns.
    fields: Dict[str, Any] = {}
    for key, value in supplied.items():
        if key == "instructions":
            fields["system_prompt"] = value
        elif key in ("knowledge", "plugins", "surfaces"):
            fields[key] = json.dumps(value)
        else:
            fields[key] = value
    if fields:
        agents_repo().update(agent_id, **fields)
    row = _live(agent_id)
    if not row:
        raise HTTPException(status_code=404, detail="agent_not_found")
    return _agent_out(row, uid=user["id"])


@router.delete("/{agent_id}", status_code=204)
async def delete_agent(agent_id: str, user: dict = Depends(get_current_user)):
    """Soft-delete an agent the caller owns, and drop its grants.

    Grants are removed too, so a later agent can never inherit a dangling
    grant through id reuse and /admin/access shows no orphan rows.

    The seeded default agent is exempt: it is infrastructure every web chat
    session is attributed to (``app/api/chat.py::_default_agent_id``), not a
    user artifact, so deleting it would make the agent vanish from the Library
    and silently reappear on the next chat. `/api/v1/agents` refuses this for
    the same reason (``agents_admin.py::delete_agent``).
    """
    row = _writable(agent_id, user)
    if row.get("is_default"):
        raise HTTPException(status_code=400, detail="default_agent_undeletable")
    agents_repo().soft_delete(agent_id)
    try:
        resource_grants_repo().delete_by_resource(_RT, agent_id)
    except Exception as e:
        logger.warning("agents: grant cleanup failed for %s: %s", agent_id, e)
    logger.info("agent deleted id=%s by=%s", agent_id, user.get("email"))
    return None
