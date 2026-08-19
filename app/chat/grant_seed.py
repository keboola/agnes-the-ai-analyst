"""First-boot seed of the chat resource grant for the Everyone group.

Chat visibility is gated on an EXPLICIT resource grant
(``app.web.router._compute_can_chat`` reads ``has_explicit_grant``,
deliberately NOT ``can_access`` — admin god-mode does not reveal chat, by
design). A fresh instance deployed with ``chat.enabled: true`` and no
``resource_grants`` row ``(group, chat, chat)`` therefore has a fully
working chat backend that is INVISIBLE to everyone, including admins —
only a hand-typed ``/chat`` URL works. This module seeds a grant for the
``Everyone`` system group once, on the instance's genuine first boot, so a
fresh deploy is usable out of the box.

``resource_grants`` carries no provenance column (unlike
``user_group_members.source`` / the ``system_seed`` convention in
``src/db.py``), so "never seeded" and "an admin deliberately revoked it"
are indistinguishable from the grants table alone. The caller
(``app.main`` lifespan) is responsible for computing
``everyone_group_preexisted`` — whether the ``Everyone`` system group
already existed *before* this boot's group-seeding step ran. That signal
can be true only once in an instance's lifetime, because system groups
are never deleted: a deliberate revoke always happens on a boot where
``Everyone`` already exists, so it can never be mistaken for a fresh
instance and silently re-seeded.

Accepted limitation (documented rather than worked around): an instance
that boots once with ``chat.enabled: false`` and turns chat on later will
NOT get the auto-seeded grant, because by then ``Everyone`` already
exists. The admin adds it once through ``/admin/access``, same as today.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def seed_everyone_chat_grant(*, everyone_group_preexisted: bool, chat_enabled: bool) -> bool:
    """Seed the ``(Everyone, chat, chat)`` resource grant on a genuinely
    fresh instance with chat enabled.

    Args:
        everyone_group_preexisted: whether the ``Everyone`` system group
            already existed before this boot's group-seeding step — the
            "is this the instance's first boot" signal (see module
            docstring). ``True`` (or unknown/uncomputable — callers should
            default conservatively to ``True``) means "not eligible".
        chat_enabled: the resolved ``chat.enabled`` value for this instance.

    Returns:
        ``True`` iff a grant was (or already is) seeded by this call,
        ``False`` otherwise (chat disabled, not a fresh instance, or the
        ``Everyone`` group could not be resolved).
    """
    if everyone_group_preexisted or not chat_enabled:
        return False

    from src.db import SYSTEM_EVERYONE_GROUP
    from src.repositories import resource_grants_repo, user_groups_repo

    everyone_group = user_groups_repo().get_by_name(SYSTEM_EVERYONE_GROUP)
    if not everyone_group:
        return False

    from app.resource_types import ResourceType

    resource_grants_repo().ensure_grant(
        group_id=everyone_group["id"],
        resource_type=ResourceType.CHAT.value,
        resource_id="chat",
        assigned_by="app.main:seed_chat_grant",
    )
    return True
