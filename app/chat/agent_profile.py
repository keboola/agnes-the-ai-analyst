"""DB-sourced agent persona profile + spawn-time scope snapshot (Task 7).

Bridges the owner-scoped `agents` repository (`src/repositories/agents.py`,
v96 schema) into the existing spawn-time `ChatProfile` mechanism
(`app/chat/profiles.py`). Two independent pieces:

1. ``build_profile`` turns an agent row into a dynamic ``ChatProfile`` —
   the same frozen dataclass the static authoring-agent profiles use — so
   ``ChatManager._spawn_live`` can materialize a persona `CLAUDE.md` +
   read-only knowledge skill into the session workdir exactly like it does
   for the hand-authored profiles in ``app/chat/profiles.py``. Returns
   ``None`` when the agent has no (non-whitespace) ``system_prompt`` — the
   seeded default agent always falls in this bucket, so a web chat session
   bound to it keeps today's generic data-analyst rails bit-for-bit.

2. ``compute_effective_scope`` / ``record_snapshot`` compute the agent's
   effective scope (which plugins/connections/tables/memory domains it is
   allowed to touch, per its four `*_mode` columns) and persist it as an
   audit row via ``agents_repo().record_scope_snapshot``.

   **V1a scope note (important): this snapshot is audit-only.** It records
   what the agent's scope *should* resolve to at spawn time so admins can
   inspect it later (``agents_repo().list_scope_snapshots``); it does NOT
   itself narrow which plugins/connections/tables/memory the live sandbox
   can actually reach — nothing in this module (or in `_spawn_live`)
   subsets the workspace materialization by the computed scope. Live seam
   enforcement (actually restricting the spawned sandbox to the scoped
   plugins/connections/tables/memory) is V1b work.

``record_snapshot`` must never raise into the spawn path: a scope-snapshot
write failure is an audit-trail gap, not a reason to fail the chat spawn
the user is waiting on. All internals are wrapped in try/except with
``logger.exception`` so a repo error (or a malformed agent row) is logged
and swallowed.

**Snapshot growth note:** ``record_snapshot`` skips the write entirely for
the default agent when all four scope modes are ``'all'`` — the seeded
default agent's baseline shape. That row's effective scope is fully
derivable from its ``*_mode`` columns alone (``{"plugins": "all", ...}``),
so persisting one identical snapshot per web-chat spawn would otherwise
accrue an unbounded, redundant `agent_scope_snapshots` row per session for
every single user — O(spawns), not O(scope changes) — with zero audit
value. Any deviation from that all-'all' shape (a non-default agent, or a
defensively-possible 'selected' mode on a default row) still gets a row,
since that *does* carry information worth auditing.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional
from uuid import uuid4

from app.chat.profiles import ChatProfile

logger = logging.getLogger(__name__)

# agents.<field>_mode -> (scope key, agent_scope.item_type)
_MODE_FIELD_TO_SCOPE = {
    "plugins_mode": ("plugins", "plugin"),
    "connections_mode": ("connections", "connection"),
    "tables_mode": ("tables", "table"),
    "memory_mode": ("memory_domains", "memory_domain"),
}


def _context_skill(agent_row: dict) -> str:
    """Render the small read-only SKILL.md describing this agent's identity.

    States the agent's name/description and that its capability is scoped
    by the owner's config — deliberately does not claim any live
    enforcement (see the module docstring's V1a/V1b split).
    """
    name = agent_row.get("name") or agent_row.get("slug") or "agent"
    description = (agent_row.get("description") or "").strip()
    slug = agent_row.get("slug") or "agent"
    body_description = description or f"Context for the '{name}' agent."
    lines = [
        "---\n",
        "name: agnes-agent-context\n",
        f"description: Identity and scope context for the '{name}' agent "
        f"({slug}) — use when you need to know who you are and what you're "
        "allowed to touch.\n",
        "---\n\n",
        f"# {name}\n\n",
        f"{body_description}\n\n",
        "This agent's capability (which plugins, connections, tables, and "
        "memory domains it may use) is scoped by its owner's configuration "
        "in Agnes, not by this file.\n",
    ]
    return "".join(lines)


def build_profile(agent_row: dict) -> Optional[ChatProfile]:
    """Build a dynamic ``ChatProfile`` from an ``agents`` row.

    Returns ``None`` when ``system_prompt`` is empty/whitespace-only — the
    caller (``ChatManager._spawn_live``) must keep today's behavior in that
    case (no profile / the static ``self._session_profiles`` lookup, if
    any), so the default agent (always an empty prompt) never changes web
    chat's generic rails.
    """
    system_prompt = (agent_row.get("system_prompt") or "").strip()
    if not system_prompt:
        return None
    slug = agent_row.get("slug") or agent_row.get("id") or "agent"
    return ChatProfile(
        slug=f"agent-{slug}",
        claude_md=system_prompt,
        skill_name="agnes-agent-context",
        skill_body=_context_skill(agent_row),
    )


def compute_effective_scope(agent_row: dict, scope_items: list[dict]) -> dict:
    """Map an agent's four scope modes + its ``agent_scope`` rows into
    ``{"plugins": [...] | "all", "connections": [...] | "all",
    "tables": [...] | "all", "memory_domains": [...] | "all"}``.

    ``mode == 'all'`` -> the literal string ``"all"``. ``mode ==
    'selected'`` -> the sorted list of ``item_id`` for that item_type drawn
    from ``scope_items`` (each a ``{"item_type": ..., "item_id": ...}``
    dict, e.g. from ``agents_repo().get_scope``).
    """
    by_type: dict[str, list[str]] = {}
    for item in scope_items:
        item_type = item.get("item_type")
        item_id = item.get("item_id")
        if item_type is None or item_id is None:
            continue
        by_type.setdefault(item_type, []).append(item_id)

    effective: dict[str, Any] = {}
    for mode_field, (scope_key, item_type) in _MODE_FIELD_TO_SCOPE.items():
        mode = agent_row.get(mode_field) or "all"
        if mode == "selected":
            effective[scope_key] = sorted(by_type.get(item_type, []))
        elif mode == "all":
            effective[scope_key] = "all"
        else:
            # Fail open (treat as "all") for backward safety, but the
            # audit trail must not silently mask config drift — an
            # unrecognized mode value means something upstream (a bad
            # migration, a hand-edited row, a future mode this code
            # doesn't know about yet) put the agent in a state this
            # function doesn't understand.
            logger.warning(
                "agent %s has unrecognized %s=%r — treating as 'all'",
                agent_row.get("id"),
                mode_field,
                mode,
            )
            effective[scope_key] = "all"
    return effective


def _is_default_all_scope(agent_row: dict) -> bool:
    """True for the (only) default-agent shape that carries no audit
    information: ``is_default`` truthy and every ``*_mode`` column is
    ``'all'``. See the module docstring's snapshot-growth note."""
    if not agent_row.get("is_default"):
        return False
    return all(agent_row.get(mode_field) == "all" for mode_field in _MODE_FIELD_TO_SCOPE)


def record_snapshot(session_id: str, agent_row: dict) -> None:
    """Compute + persist an audit-only scope snapshot for this spawn.

    Skips the write for the default agent when its scope is fully-'all'
    (see the module docstring's snapshot-growth note) — that case is
    intentionally not an audit gap, just nothing worth recording.

    Never raises — see the module docstring. A failure here (repo error,
    coordination hiccup, malformed row) is logged and swallowed so it can
    never take down the chat spawn that is waiting on this call.
    """
    try:
        if _is_default_all_scope(agent_row):
            return

        from src.repositories import agents_repo

        agent_id = agent_row["id"]
        repo = agents_repo()
        scope_items = repo.get_scope(agent_id)
        effective_scope = compute_effective_scope(agent_row, scope_items)
        repo.record_scope_snapshot(
            id=str(uuid4()),
            session_id=session_id,
            agent_id=agent_id,
            effective_scope=json.dumps(effective_scope, sort_keys=True),
        )
    except Exception:
        logger.exception(
            "agent scope snapshot failed for session %s — spawn continues without an audit row",
            session_id,
        )
