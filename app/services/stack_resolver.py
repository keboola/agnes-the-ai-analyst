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
    # v51: lifecycle ``status`` (drives hero filter checkboxes + cover
    # status pill) and ``category`` (drives card eyebrow line — Data
    # Packages only; Memory Domains pass None).
    status: Optional[str] = "prod"
    category: Optional[str] = None
    # v56: extended content surfaced on the Browse-grid card. Owner
    # renders as a small chip; tags as inline pills; badges (curated /
    # new) derived in :meth:`_fetch_entries` from the creator's group
    # membership + ``created_at`` age.
    owner_name: Optional[str] = None
    owner_team: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    badges: List[str] = field(default_factory=list)
    requirement: Literal["available", "required"] = "available"
    in_stack: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


class StackResolver:
    """Composes ``resource_grants`` ∪ ``user_stack_subscriptions`` →
    effective stack per resource type.

    Backend-aware: every read/write routes through the repository factory
    (``src.repositories``), so the resolver hits whichever backend
    (DuckDB or Postgres) the process is configured for.

    The legacy ``conn`` argument is retained as a *test-isolation escape
    hatch*: when the active backend is DuckDB **and** a DuckDB connection
    is supplied, the resolver reads/writes through that connection (so a
    unit test seeding an in-memory ``:memory:`` DuckDB sees its own data).
    When the backend is Postgres the ``conn`` is ignored and the factory
    routes to PG. This mirrors the same pattern in ``app.auth.access``
    (``_user_group_ids`` / ``can_access``).
    """

    def __init__(self, conn: Any = None):
        self.conn = conn

    # -- Repo accessors (honor the conn escape hatch) ----------------------

    def _use_local_conn(self) -> bool:
        """True iff we should read/write through ``self.conn`` directly.

        Only when a connection was supplied AND the active backend is
        DuckDB — otherwise the factory owns backend selection.
        """
        if self.conn is None:
            return False
        from src.repositories import use_pg
        return not use_pg()

    def _members_repo(self) -> Any:
        if self._use_local_conn():
            from src.repositories.user_group_members import (
                UserGroupMembersRepository,
            )
            return UserGroupMembersRepository(self.conn)
        from src.repositories import user_group_members_repo
        return user_group_members_repo()

    def _grants_repo(self) -> Any:
        if self._use_local_conn():
            from src.repositories.resource_grants import ResourceGrantsRepository
            return ResourceGrantsRepository(self.conn)
        from src.repositories import resource_grants_repo
        return resource_grants_repo()

    def _subscriptions_repo(self) -> Any:
        if self._use_local_conn():
            from src.repositories.user_stack_subscriptions import (
                UserStackSubscriptionsRepository,
            )
            return UserStackSubscriptionsRepository(self.conn)
        from src.repositories import user_stack_subscriptions_repo
        return user_stack_subscriptions_repo()

    def _groups_repo(self) -> Any:
        if self._use_local_conn():
            from src.repositories.user_groups import UserGroupsRepository
            return UserGroupsRepository(self.conn)
        from src.repositories import user_groups_repo
        return user_groups_repo()

    def _data_packages_repo(self) -> Any:
        if self._use_local_conn():
            from src.repositories.data_packages import DataPackagesRepository
            return DataPackagesRepository(self.conn)
        from src.repositories import data_packages_repo
        return data_packages_repo()

    def _memory_domains_repo(self) -> Any:
        if self._use_local_conn():
            from src.repositories.memory_domains import MemoryDomainsRepository
            return MemoryDomainsRepository(self.conn)
        from src.repositories import memory_domains_repo
        return memory_domains_repo()

    # -- Group + grant lookups (private) -----------------------------------

    def _user_group_ids(self, user_id: str) -> List[str]:
        return self._members_repo().list_groups_for_user(user_id)

    def _grants(
        self, group_ids: List[str], resource_type: ResourceType
    ) -> Tuple[set, set]:
        """Split (required, available) resource_id sets for the user's groups.

        Empty group_ids → ({}, {}); the resolver short-circuits to "no
        entries" for both browse() and stack().
        """
        if not group_ids:
            return set(), set()
        rows = self._grants_repo().list_for_groups(
            list(group_ids), str(resource_type)
        )
        required_ids = {
            r["resource_id"] for r in rows if r.get("requirement") == "required"
        }
        available_ids = {
            r["resource_id"] for r in rows if r.get("requirement") == "available"
        }
        # Per Section 4.3 — if an id appears in both buckets across grants,
        # the required one wins. Remove it from available to keep the
        # union math clean (subscribed_ids ∩ available_ids).
        available_ids -= required_ids
        return required_ids, available_ids

    def _subscribed_ids(
        self, user_id: str, resource_type: ResourceType
    ) -> set:
        return set(
            self._subscriptions_repo().list_for_user(
                user_id, str(resource_type)
            )
        )

    # -- Public API --------------------------------------------------------

    def stack(
        self, user_id_or_principal, resource_type: ResourceType
    ) -> List[ResourceEntry]:
        """The user's effective stack — required ∪ (subscribed ∩ available)
        for regular users; admin (god-mode) gets ALL their subscriptions
        regardless of group grants, because admins legitimately POST
        /api/stack/subscribe without first granting themselves a group.
        Filtering admin's subscriptions through the available-grant join
        was the "Add to stack worked but My Stack stays empty" bug.

        Also accepts a ``SessionPrincipal``: returns only the resources whose
        ids are in the co-session's intersection (no admin path, no subscription
        lookup). Every returned entry is marked ``in_stack=True``.
        """
        from app.auth.session_principal import SessionPrincipal
        if isinstance(user_id_or_principal, SessionPrincipal):
            ids = user_id_or_principal.intersection.get(resource_type.value, frozenset())
            entries = self._fetch_entries(resource_type, set(ids), set(ids))
            for e in entries:
                e.in_stack = True
            return entries
        user_id = user_id_or_principal
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
        Admin uses :meth:`browse_admin` for the full list; this method
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

    def browse_admin(
        self, user_id: str, resource_type: ResourceType
    ) -> List[ResourceEntry]:
        """Admin god-mode Browse: ALL entries of ``resource_type`` with
        v51/v56 enrichment (status, category, owner_name, tags, badges).

        ``requirement`` reflects the admin's OWN group grants — required
        packages are still rendered with the disabled "In stack
        (required)" footer button so the admin sees what regular users
        in those groups see, and the macro doesn't render an actionable
        Remove button that the API would 400 on. ``in_stack`` reflects
        the admin's own subscriptions (required entries are also always
        in_stack by convention — required ⇒ in stack).
        """
        # Soft-deleted entries (``deleted_at IS NOT NULL``) are excluded
        # from admin Browse — they're still in the DB for the Undo
        # window but a /catalog or /memory render mustn't surface them.
        if resource_type == ResourceType.DATA_PACKAGE:
            all_ids = {
                r["id"] for r in self._data_packages_repo().list(limit=100000)
            }
        elif resource_type == ResourceType.MEMORY_DOMAIN:
            all_ids = {
                r["id"] for r in self._memory_domains_repo().list(limit=100000)
            }
        else:
            raise ValueError(
                f"browse_admin does not support resource_type={resource_type!r}"
            )
        groups = self._user_group_ids(user_id)
        required_ids, _ = self._grants(groups, resource_type)
        subscribed_ids = self._subscribed_ids(user_id, resource_type)
        entries = self._fetch_entries(resource_type, all_ids, required_ids)
        for e in entries:
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

        NOTE: this method does NOT verify the user has an ``available``
        grant for the resource. Authorization is enforced at the API
        layer by ``app/api/stack.py``'s ``can_access`` gate. Direct
        in-process callers (tests, admin scripts) are trusted to have
        gated themselves; ``stack()`` further hides any resulting
        subscription on every read by intersecting with current
        available_ids, so a zombie row never leaks into the user-
        facing manifest.
        """
        if self.is_required(user_id, resource_type, resource_id):
            raise HTTPException(status_code=400, detail="already_required")
        self._subscriptions_repo().subscribe(
            user_id, str(resource_type), resource_id
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
        self._subscriptions_repo().unsubscribe(
            user_id, str(resource_type), resource_id
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
        rows = [
            r
            for r in self._grants_repo().list_for_groups(
                list(groups), "memory_item"
            )
            if r["resource_id"] == item_id
        ]
        if not rows:
            return item_is_required
        # Per-group grants exist → they override the global flag.
        # Within the per-group layer, required wins over available.
        requirements = {r.get("requirement") for r in rows}
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
        # v51: status + category. Memory Domains have status but no category
        # (the repo dict simply lacks the key → None). v56: data_packages
        # carry extended content (owner_name, tags) plus badge inputs
        # (created_by + created_at); memory domains stay v51-shaped.
        # Resolver-level badge derivation matches the API's _badges_for()
        # heuristic: 'curated' iff creator is in Admin, 'new' iff created_at
        # < 30 days ago. Soft-deleted entries are already excluded by the
        # repos' ``list()`` (``deleted_at IS NULL``), so a grant whose
        # target was deleted via /admin/* doesn't pull the row back.
        if resource_type == ResourceType.DATA_PACKAGE:
            rows = [
                r for r in self._data_packages_repo().list(limit=100000)
                if r["id"] in ids
            ]
        elif resource_type == ResourceType.MEMORY_DOMAIN:
            rows = [
                r for r in self._memory_domains_repo().list(limit=100000)
                if r["id"] in ids
            ]
        else:
            raise ValueError(
                f"StackResolver does not support resource_type={resource_type!r}"
            )

        from datetime import datetime, timedelta, timezone as _tz
        import json as _json

        admin_keys = self._admin_keys()

        now = datetime.now(_tz.utc)
        entries: List[ResourceEntry] = []
        for r in rows:
            tags_raw = r.get("tags")
            if isinstance(tags_raw, str) and tags_raw:
                try:
                    tags_list = _json.loads(tags_raw)
                    if not isinstance(tags_list, list):
                        tags_list = []
                except Exception:
                    tags_list = []
            elif isinstance(tags_raw, list):
                tags_list = tags_raw
            else:
                tags_list = []

            badges: List[str] = []
            created_by = r.get("created_by")
            if created_by and created_by in admin_keys:
                badges.append("curated")
            created_at = r.get("created_at")
            if isinstance(created_at, datetime):
                ts = created_at if created_at.tzinfo else created_at.replace(tzinfo=_tz.utc)
                if (now - ts) < timedelta(days=30):
                    badges.append("new")

            rid = r["id"]
            entries.append(ResourceEntry(
                id=rid,
                name=r.get("name"),
                description=r.get("description"),
                icon=r.get("icon"),
                color=r.get("color"),
                cover_image_url=r.get("cover_image_url"),
                status=r.get("status") or "prod",
                category=r.get("category"),
                owner_name=r.get("owner_name"),
                owner_team=r.get("owner_team"),
                tags=tags_list,
                badges=badges,
                requirement=(
                    "required" if rid in required_ids else "available"
                ),
            ))
        # The repos return name-ordered rows already; keep that order.
        return entries

    def _admin_keys(self) -> set:
        """Admin group's member emails + ids, used for the 'curated' badge.

        Best-effort: returns an empty set on any lookup failure so badge
        derivation never breaks an entry fetch.
        """
        keys: set = set()
        try:
            from src.db import SYSTEM_ADMIN_GROUP
            admin = self._groups_repo().get_by_name(SYSTEM_ADMIN_GROUP)
            if not admin:
                return keys
            for member in self._members_repo().list_members_for_group(
                admin["id"]
            ):
                if member.get("email"):
                    keys.add(member["email"])
                if member.get("id"):
                    keys.add(member["id"])
        except Exception:
            pass
        return keys
