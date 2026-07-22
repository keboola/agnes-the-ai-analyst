"""Agent management API — `/api/v1/agents` (CRUD + scope + agent-PAT issuance).

Owner-scoped CRUD over agent profiles (v96, `docs/superpowers/specs/
2026-07-21-agent-profiles-and-agent-api-design.md` §2), per-agent scope
grants, and agent PAT issuance. Every route requires an interactive session —
`require_session_token` already rejects every PAT flavor (plain PAT and
agent PAT alike), matching the spec's "Management endpoints require
interactive owner auth" rule.

Ownership (normative, per the spec's auth matrix):
  - non-owner, non-admin -> 404 on every `{id}` route (existence is not
    leaked to a caller who isn't the owner).
  - admin -> GET allowed (read-only governance); mutations and token
    minting on a foreign agent -> 403 (admin never mints a PAT for
    someone else's agent).

Agent PATs cannot be issued for `'all'`-mode agents (including the default
agent) — token issuance requires all four scope modes to be `'selected'`.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import duckdb
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import exc as sa_exc

from app.auth.access import is_user_admin
from app.auth.dependencies import _get_db, require_session_token
from app.auth.jwt import create_access_token
from src.repositories import access_token_repo, agents_repo, audit_repo

router = APIRouter(prefix="/api/v1/agents", tags=["agents"])

# Lowercase kebab-case, max 64 chars. "default" is reserved for the one
# seeded-per-owner default agent (created via `agents_repo().get_or_create_default`,
# never through this API).
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_RESERVED_SLUGS = frozenset({"default"})

_SELECTED_MODE_FIELDS = ("plugins_mode", "connections_mode", "tables_mode", "memory_mode")
_ITEM_TYPES = frozenset({"plugin", "connection", "table", "memory_domain"})

# Mirrors `src.repositories.agents._UPDATABLE` minus `slug` (immutable via
# this API — see `update_agent`).
_UPDATABLE_FIELDS = frozenset(
    {
        "name",
        "description",
        "system_prompt",
        "model",
        "token_budget_monthly",
        "plugins_mode",
        "connections_mode",
        "tables_mode",
        "memory_mode",
        "memory_write_mode",
    }
)


class CreateAgentRequest(BaseModel):
    name: str
    slug: str
    description: Optional[str] = None
    system_prompt: Optional[str] = None
    model: Optional[str] = None
    token_budget_monthly: Optional[int] = None


class UpdateAgentRequest(BaseModel):
    name: Optional[str] = None
    # Accepted only so a PUT that supplies it can be rejected with
    # `slug_immutable` — never applied.
    slug: Optional[str] = None
    description: Optional[str] = None
    system_prompt: Optional[str] = None
    model: Optional[str] = None
    token_budget_monthly: Optional[int] = None
    plugins_mode: Optional[str] = None
    connections_mode: Optional[str] = None
    tables_mode: Optional[str] = None
    memory_mode: Optional[str] = None
    memory_write_mode: Optional[str] = None


class ScopeItem(BaseModel):
    item_type: str
    item_id: str


class SetScopeRequest(BaseModel):
    items: List[ScopeItem] = []


class CreateAgentTokenRequest(BaseModel):
    name: str
    expires_in_days: Optional[int] = 90  # null = no expiry


def _err(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _audit(actor: str, action: str, target: str, params: Optional[dict] = None) -> None:
    try:
        audit_repo().log(user_id=actor, action=action, resource=f"agent:{target}", params=params)
    except Exception:
        pass


def _serialize(row: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(row)
    for key in ("created_at", "updated_at", "deleted_at"):
        if out.get(key) is not None:
            out[key] = str(out[key])
    return out


def _validate_new_slug(slug: str) -> None:
    if not _SLUG_RE.match(slug):
        raise _err(
            400,
            "invalid_slug",
            "slug must be lowercase kebab-case (^[a-z0-9][a-z0-9-]{0,63}$)",
        )
    if slug in _RESERVED_SLUGS:
        raise _err(400, "slug_reserved", f"slug '{slug}' is reserved for the seeded default agent")


def _load_agent(
    agent_id: str,
    user: dict,
    conn: Optional[duckdb.DuckDBPyConnection],
    *,
    require_owner: bool,
) -> Dict[str, Any]:
    """Fetch `agent_id`, enforcing the ownership/admin auth matrix.

    404s for anyone who isn't the owner or an admin — existence of another
    user's agent is never leaked. Admins pass the existence check (so GET
    works for governance) but `require_owner=True` (every mutating route,
    including token issuance) still 403s them on a foreign agent.
    """
    row = agents_repo().get_by_id(agent_id)
    if not row or row.get("deleted_at") is not None:
        raise _err(404, "agent_not_found", "Agent not found")
    is_owner = row["owner_user_id"] == user["id"]
    if not is_owner:
        if not is_user_admin(user["id"], conn):
            raise _err(404, "agent_not_found", "Agent not found")
        if require_owner:
            raise _err(403, "agent_not_owned", "Admins may inspect but not modify another user's agent")
    return row


@router.post("", status_code=201)
async def create_agent(
    payload: CreateAgentRequest,
    user: dict = Depends(require_session_token),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    name = payload.name.strip()
    if not name:
        raise _err(400, "invalid_name", "name is required")
    slug = payload.slug.strip()
    _validate_new_slug(slug)

    repo = agents_repo()
    if repo.get_by_slug(user["id"], slug) is not None:
        raise _err(409, "slug_taken", f"slug '{slug}' is already in use")

    agent_id = str(uuid.uuid4())
    try:
        # API-created agents default all four scope modes to 'selected'
        # (spec §1) — the repo's own defaults are 'all', which is only
        # correct for the seeded default agent, so pass them explicitly.
        repo.create(
            id=agent_id,
            owner_user_id=user["id"],
            name=name,
            slug=slug,
            description=payload.description,
            system_prompt=payload.system_prompt,
            model=payload.model,
            token_budget_monthly=payload.token_budget_monthly,
            plugins_mode="selected",
            connections_mode="selected",
            tables_mode="selected",
            memory_mode="selected",
        )
    except (duckdb.ConstraintException, sa_exc.IntegrityError):
        # Covers the tombstoned-slug race the pre-check above can't see
        # (get_by_slug only matches deleted_at IS NULL rows) — UNIQUE
        # (owner_user_id, slug) is unconditional, so a tombstoned slug still
        # raises here. DuckDB raises ConstraintException, Postgres (via
        # SQLAlchemy) raises IntegrityError — anything else is a genuine
        # 500, not a slug conflict, and must propagate.
        raise _err(409, "slug_taken", f"slug '{slug}' is already in use")

    row = repo.get_by_id(agent_id)
    _audit(user["id"], "agent.create", agent_id, {"slug": slug})
    return _serialize(row)  # type: ignore[arg-type]


@router.get("")
async def list_agents(user: dict = Depends(require_session_token)):
    rows = agents_repo().list_for_user(user["id"])
    return {"data": [_serialize(r) for r in rows], "has_more": False, "next_cursor": None}


@router.get("/{agent_id}")
async def get_agent(
    agent_id: str,
    user: dict = Depends(require_session_token),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    row = _load_agent(agent_id, user, conn, require_owner=False)
    return _serialize(row)


@router.put("/{agent_id}")
async def update_agent(
    agent_id: str,
    payload: UpdateAgentRequest,
    user: dict = Depends(require_session_token),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    _load_agent(agent_id, user, conn, require_owner=True)

    updates = payload.model_dump(exclude_unset=True)
    if "slug" in updates:
        raise _err(400, "slug_immutable", "slug cannot be changed after creation")

    # Belt-and-suspenders: today `set(updates)` (Pydantic's exclude_unset
    # field set, minus slug) is always a subset of _UPDATABLE_FIELDS by
    # construction — UpdateAgentRequest declares no other fields. Keeps this
    # guard live so a future field added to the request model without a
    # matching _UPDATABLE_FIELDS entry fails loudly instead of silently
    # reaching `agents_repo().update()`.
    bad = set(updates) - _UPDATABLE_FIELDS
    if bad:
        raise _err(400, "invalid_field", f"cannot update field(s): {sorted(bad)}")

    if updates:
        agents_repo().update(agent_id, **updates)
        _audit(user["id"], "agent.update", agent_id, {"fields": sorted(updates)})

    return _serialize(agents_repo().get_by_id(agent_id))  # type: ignore[arg-type]


@router.delete("/{agent_id}", status_code=204)
async def delete_agent(
    agent_id: str,
    user: dict = Depends(require_session_token),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    row = _load_agent(agent_id, user, conn, require_owner=True)
    if row.get("is_default"):
        raise _err(400, "default_agent_undeletable", "the default agent cannot be deleted")

    agents_repo().soft_delete(agent_id)
    # Revoke every PAT minted for this agent — a deleted agent must not
    # leave live credentials behind. Belt-and-suspenders, not the sole
    # guard: `soft_delete` and `revoke_for_agent` are two separate
    # connections/transactions, so if this call fails after the soft-delete
    # above already committed, an orphaned agent PAT would otherwise keep
    # authenticating. `app.auth.pat_resolver.resolve_token_to_user` closes
    # that gap independently — on the `typ="agent_pat"` path it loads the
    # agent row by the JWT's `agent_id` claim and rejects
    # (`"agent_pat_agent_deleted"`) when it is missing or soft-deleted, so a
    # deleted agent's PAT dies even if this revoke call never runs.
    access_token_repo().revoke_for_agent(agent_id)
    _audit(user["id"], "agent.delete", agent_id)


@router.put("/{agent_id}/scope")
async def set_agent_scope(
    agent_id: str,
    payload: SetScopeRequest,
    user: dict = Depends(require_session_token),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    _load_agent(agent_id, user, conn, require_owner=True)

    items: List[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in payload.items:
        if item.item_type not in _ITEM_TYPES:
            raise _err(
                400,
                "invalid_item_type",
                f"item_type must be one of {sorted(_ITEM_TYPES)}, got '{item.item_type}'",
            )
        key = (item.item_type, item.item_id)
        # Dedupe (item_type, item_id) pairs, preserving first-seen order —
        # the composite PK on `agent_scope` means a duplicate pair in one
        # request would otherwise 500 on the second INSERT.
        if key in seen:
            continue
        seen.add(key)
        items.append(key)

    agents_repo().set_scope(agent_id, items)
    _audit(user["id"], "agent.scope.set", agent_id, {"count": len(items)})
    return {"items": [{"item_type": t, "item_id": i} for t, i in items]}


@router.post("/{agent_id}/tokens")
async def create_agent_token(
    agent_id: str,
    payload: CreateAgentTokenRequest,
    user: dict = Depends(require_session_token),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    row = _load_agent(agent_id, user, conn, require_owner=True)

    if not all(row.get(field) == "selected" for field in _SELECTED_MODE_FIELDS):
        raise _err(
            403,
            "agent_not_selected_mode",
            "agent PATs require all four scope modes (plugins/connections/tables/memory) "
            "to be 'selected' — never for 'all'-mode agents",
        )

    name = payload.name.strip()
    if not name:
        raise _err(400, "invalid_name", "name is required")
    if payload.expires_in_days is not None and payload.expires_in_days <= 0:
        raise _err(400, "invalid_expiry", "expires_in_days must be a positive integer")
    if payload.expires_in_days is not None and payload.expires_in_days > 3650:
        raise _err(400, "invalid_expiry", "expires_in_days must not exceed 3650 (10 years)")

    omit_exp = payload.expires_in_days is None
    expires_delta = timedelta(days=payload.expires_in_days) if payload.expires_in_days is not None else None
    expires_at = datetime.now(timezone.utc) + expires_delta if expires_delta is not None else None

    # jti / prefix / hash mechanics mirror app/api/tokens.py::create_token
    # exactly. CRITICAL: the DB row's agent_id and the JWT's agent_id claim
    # must both be set to the SAME agent_id — kept in sync by construction
    # here (one `agent_id` local var feeds both `extra_claims` and
    # `repo.create`), not by two independently-derived values.
    token_id = str(uuid.uuid4())
    jwt_token = create_access_token(
        user_id=user["id"],
        email=user["email"],
        token_id=token_id,
        typ="agent_pat",
        expires_delta=expires_delta,
        omit_exp=omit_exp,
        extra_claims={"agent_id": agent_id},
    )
    prefix = token_id.replace("-", "")[:8]
    token_hash = hashlib.sha256(jwt_token.encode()).hexdigest()
    access_token_repo().create(
        id=token_id,
        user_id=user["id"],
        name=name,
        token_hash=token_hash,
        prefix=prefix,
        expires_at=expires_at,
        agent_id=agent_id,
    )
    _audit(user["id"], "agent.token.create", token_id, {"agent_id": agent_id, "name": name})

    return {
        "id": token_id,
        "name": name,
        "prefix": prefix,
        "agent_id": agent_id,
        "token": jwt_token,  # returned EXACTLY ONCE; never retrievable again
        "expires_at": str(expires_at) if expires_at else None,
        "created_at": str(datetime.now(timezone.utc)),
    }
