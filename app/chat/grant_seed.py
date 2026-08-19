"""One-time seed of the chat resource grant for the Everyone group.

Chat visibility is gated on an EXPLICIT resource grant
(``app.web.router._compute_can_chat`` reads ``has_explicit_grant``,
deliberately NOT ``can_access`` — admin god-mode does not reveal chat, by
design). A fresh instance deployed with ``chat.enabled: true`` and no
``resource_grants`` row ``(group, chat, chat)`` therefore has a fully
working chat backend that is INVISIBLE to everyone, including admins —
only a hand-typed ``/chat`` URL works. This module seeds that grant once
so a fresh deploy is usable out of the box.

Seeding exactly once is the whole difficulty: ``resource_grants`` has no
provenance column, so "never seeded" and "an admin deliberately revoked
it" look identical in the table. A marker file in the state directory
records that we have seeded, which makes the two distinguishable without
a schema change — the same approach ``app.secrets`` uses for the
generated ``.session_secret`` next to it, and it lives on the persistent
data disk, so it survives container recreates and auto-upgrades.

Deliberately keyed on the marker rather than on "is this the instance's
first boot": an instance that boots with chat disabled and turns it on
later still gets the grant the first time it boots WITH chat enabled,
because nothing was seeded (and no marker written) before that.

Multi-replica caveat: replicas that do not share the state directory
would each seed once. The insert is idempotent, so the only visible
effect is that a revoked grant could come back on a replica that never
seeded it — acceptable, and impossible in the single-mount deployments
the module provisions.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: Written next to `.session_secret` on the persistent state volume.
MARKER_NAME = ".chat_grant_seeded"


def seed_everyone_chat_grant(*, chat_enabled: bool) -> bool:
    """Seed the ``(Everyone, chat, chat)`` grant unless it was seeded before.

    Args:
        chat_enabled: the resolved ``chat.enabled`` value for this instance.
            When false nothing is seeded and no marker is written, so the
            grant is still seeded the first time the instance boots with
            chat turned on.

    Returns:
        ``True`` iff this call seeded the grant, ``False`` otherwise (chat
        disabled, already seeded once, or the ``Everyone`` group could not
        be resolved).
    """
    if not chat_enabled:
        return False

    from app.secrets import _state_dir

    marker = _state_dir() / MARKER_NAME
    if marker.exists():
        return False

    from src.db import SYSTEM_EVERYONE_GROUP
    from src.repositories import resource_grants_repo, user_groups_repo

    everyone_group = user_groups_repo().get_by_name(SYSTEM_EVERYONE_GROUP)
    if not everyone_group:
        # Too early, or a backend where the group seed has not run yet.
        # Leave the marker unwritten so the next boot retries.
        return False

    from app.resource_types import ResourceType

    resource_grants_repo().ensure_grant(
        group_id=everyone_group["id"],
        resource_type=ResourceType.CHAT.value,
        resource_id="chat",
        assigned_by="app.main:seed_chat_grant",
    )

    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("seeded by app.main:seed_chat_grant\n", encoding="utf-8")
    return True
