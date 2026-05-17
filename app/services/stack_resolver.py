"""StackResolver — unified browse + stack + required resolver (v49).

Scope: ``DATA_PACKAGE`` + ``MEMORY_DOMAIN`` resource types, plus a
``MEMORY_ITEM`` helper for item-level Required override. Marketplace
plugins keep their own resolver in ``src/marketplace_filter.py`` per
design D1.

Resolution algorithm (Section 4.2 of the design doc):

    groups          := user_group_members(user_id).group_id
    grants          := resource_grants WHERE group_id IN groups AND resource_type = T
    required_ids    := {g.resource_id | g in grants if g.requirement = 'required'}
    available_ids   := {g.resource_id | g in grants if g.requirement = 'available'}
    subscribed_ids  := user_stack_subscriptions(user_id, T).resource_id ∩ available_ids
    effective_ids   := required_ids ∪ subscribed_ids
    return fetch_entries(T, effective_ids)

Required precedence (Section 4.3): any ``required`` grant beats every
``available`` grant for the same (user, resource_id) pair. We compute this
by set-union: an id present in ``required_ids`` is required regardless of
what other grants on the same id say.

Memory item-level Required precedence (Section 4.4): per-group MEMORY_ITEM
grants override the global ``knowledge_items.is_required`` flag. See the
``memory_item_is_required`` method docstring for the full rule.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Tuple

import duckdb
from fastapi import HTTPException

from app.resource_types import ResourceType


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


@dataclass
class ResourceEntry:
    """One row in the browse/stack response.

    ``requirement`` reflects the effective requirement after the OR-across-
    grants rule. ``in_stack`` is True iff the resource is in the user's
    effective stack (``required`` always counts as in_stack; ``available``
    requires an explicit subscription).
    """

    id: str
    name: str
    description: Optional[str] = None
    icon: Optional[str] = None
    color: Optional[str] = None
    # v50: optional admin-uploaded cover image URL (served from /uploads/).
    # When set the card renders an <img>; when None the card falls back to
    # the flat-color + initials banner. Symmetric for Data Packages and
    # Memory Domains.
    cover_image_url: Optional[str] = None
    requirement: Literal["available", "required"] = "available"
    in_stack: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


class StackResolver:
    """Composes ``resource_grants`` ∪ ``user_stack_subscriptions`` →
    effective stack per resource type."""

    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    # -- Group + grant lookups (private) -----------------------------------

    def _user_group_ids(self, user_id: str) -> List[str]:
        rows = self.conn.execute(
            "SELECT group_id FROM user_group_members WHERE user_id = ?",
            [user_id],
        ).fetchall()
        return [r[0] for r in rows]

    def _grants(
        self, group_ids: List[str], resource_type: ResourceType
    ) -> Tuple[set, set]:
        """Split (required, available) resource_id sets for the user's groups.

        Empty group_ids → ({}, {}); the resolver short-circuits to "no
        entries" for both browse() and stack().
        """
        if not group_ids:
            return set(), set()
        placeholders = ",".join(["?"] * len(group_ids))
        rows = self.conn.execute(
            f"""
            SELECT resource_id, requirement
              FROM resource_grants
             WHERE group_id IN ({placeholders})
               AND resource_type = ?
            """,
            [*group_ids, str(resource_type)],
        ).fetchall()
        required_ids = {r[0] for r in rows if r[1] == "required"}
        available_ids = {r[0] for r in rows if r[1] == "available"}
        # Per Section 4.3 — if an id appears in both buckets across grants,
        # the required one wins. Remove it from available to keep the
        # union math clean (subscribed_ids ∩ available_ids).
        available_ids -= required_ids
        return required_ids, available_ids

    def _subscribed_ids(
        self, user_id: str, resource_type: ResourceType
    ) -> set:
        rows = self.conn.execute(
            "SELECT resource_id FROM user_stack_subscriptions "
            "WHERE user_id = ? AND resource_type = ?",
            [user_id, str(resource_type)],
        ).fetchall()
        return {r[0] for r in rows}

    # -- Public API --------------------------------------------------------

    def stack(
        self, user_id: str, resource_type: ResourceType
    ) -> List[ResourceEntry]:
        """The user's effective stack — required ∪ (subscribed ∩ available)
        for regular users; admin (god-mode) gets ALL their subscriptions
        regardless of group grants, because admins legitimately POST
        /api/stack/subscribe without first granting themselves a group.
        Filtering admin's subscriptions through the available-grant join
        was the "Add to stack worked but My Stack stays empty" bug."""
        groups = self._user_group_ids(user_id)
        required_ids, available_ids = self._grants(groups, resource_type)
        raw_subscribed = self._subscribed_ids(user_id, resource_type)
        # Admin god-mode: zombie-subscription protection doesn't apply —
        # admin sees all their actual subscriptions even without a grant.
        from app.auth.access import is_user_admin
        admin_bypass = is_user_admin(user_id, self.conn)
        subscribed_ids = raw_subscribed if admin_bypass else (raw_subscribed & available_ids)
        effective_ids = required_ids | subscribed_ids
        entries = self._fetch_entries(resource_type, effective_ids, required_ids)
        # In stack() every entry is by definition in_stack=True.
        for e in entries:
            e.in_stack = True
        return entries

    def browse(
        self, user_id: str, resource_type: ResourceType
    ) -> List[ResourceEntry]:
        """All resources the user could see — required + available, annotated
        with ``in_stack`` so the UI can render Add/Remove affordances.
        Admin uses route-handler god-mode for the full list; this method
        stays grants-based so non-admin browse is correct."""
        groups = self._user_group_ids(user_id)
        required_ids, available_ids = self._grants(groups, resource_type)
        all_ids = required_ids | available_ids
        subscribed_ids = self._subscribed_ids(user_id, resource_type)
        entries = self._fetch_entries(resource_type, all_ids, required_ids)
        for e in entries:
            # required → always in stack; available → only when subscribed.
            e.in_stack = e.id in required_ids or e.id in subscribed_ids
        return entries

    def is_required(
        self,
        user_id: str,
        resource_type: ResourceType,
        resource_id: str,
    ) -> bool:
        """True iff ANY of the user's groups has a ``required`` grant for
        this resource (Section 4.3 OR rule)."""
        groups = self._user_group_ids(user_id)
        required_ids, _ = self._grants(groups, resource_type)
        return resource_id in required_ids

    def add_to_stack(
        self,
        user_id: str,
        resource_type: ResourceType,
        resource_id: str,
    ) -> None:
        """Subscribe the user to an ``available`` resource.

        Raises HTTP 400 if the resource is already ``required`` — clients
        shouldn't try to subscribe to a required resource (it's in the
        stack by default).
        """
        if self.is_required(user_id, resource_type, resource_id):
            raise HTTPException(status_code=400, detail="already_required")
        self.conn.execute(
            "INSERT INTO user_stack_subscriptions"
            "(user_id, resource_type, resource_id) "
            "VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
            [user_id, str(resource_type), resource_id],
        )

    def remove_from_stack(
        self,
        user_id: str,
        resource_type: ResourceType,
        resource_id: str,
    ) -> None:
        """Drop the subscription.

        Raises HTTP 400 if the resource is ``required`` — users can't opt
        out of required grants.
        """
        if self.is_required(user_id, resource_type, resource_id):
            raise HTTPException(
                status_code=400, detail="cannot_remove_required"
            )
        self.conn.execute(
            "DELETE FROM user_stack_subscriptions "
            "WHERE user_id = ? AND resource_type = ? AND resource_id = ?",
            [user_id, str(resource_type), resource_id],
        )

    # -- Memory item-level resolver (Section 4.4) --------------------------

    def memory_item_is_required(
        self,
        user_id: str,
        item_id: str,
        item_is_required: bool,
    ) -> bool:
        """Per-user effective is_required flag for a single memory item.

        Precedence (top-down):
        1. Any group grant ``MEMORY_ITEM, required`` for this item → True
        2. Any group grant ``MEMORY_ITEM, available`` for this item → False
           (per-group override "this item is NOT required for our group")
        3. Item's global ``knowledge_items.is_required = TRUE`` → True
        4. Otherwise → False

        The required→available precedence within the per-group layer
        follows Section 4.3 (required OR). Both required and available
        per-group grants override the global flag.
        """
        groups = self._user_group_ids(user_id)
        if not groups:
            return item_is_required
        placeholders = ",".join(["?"] * len(groups))
        rows = self.conn.execute(
            f"""
            SELECT requirement FROM resource_grants
             WHERE group_id IN ({placeholders})
               AND resource_type = 'memory_item'
               AND resource_id   = ?
            """,
            [*groups, item_id],
        ).fetchall()
        if not rows:
            return item_is_required
        # Per-group grants exist → they override the global flag.
        # Within the per-group layer, required wins over available.
        requirements = {r[0] for r in rows}
        return "required" in requirements

    # -- Domain entry fetch (private) --------------------------------------

    def _fetch_entries(
        self,
        resource_type: ResourceType,
        ids: set,
        required_ids: set,
    ) -> List[ResourceEntry]:
        if not ids:
            return []
        placeholders = ",".join(["?"] * len(ids))
        if resource_type == ResourceType.DATA_PACKAGE:
            rows = self.conn.execute(
                f"""SELECT id, name, description, icon, color, cover_image_url
                       FROM data_packages WHERE id IN ({placeholders})
                       ORDER BY name""",
                list(ids),
            ).fetchall()
        elif resource_type == ResourceType.MEMORY_DOMAIN:
            rows = self.conn.execute(
                f"""SELECT id, name, description, icon, color, cover_image_url
                       FROM memory_domains WHERE id IN ({placeholders})
                       ORDER BY name""",
                list(ids),
            ).fetchall()
        else:
            raise ValueError(
                f"StackResolver does not support resource_type={resource_type!r}"
            )
        return [
            ResourceEntry(
                id=r[0], name=r[1], description=r[2], icon=r[3], color=r[4],
                cover_image_url=r[5],
                requirement=(
                    "required" if r[0] in required_ids else "available"
                ),
            )
            for r in rows
        ]
