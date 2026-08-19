"""Cross-engine contract test for the first-boot chat resource grant seed.

Chat visibility is gated on an EXPLICIT ``resource_grants`` row
(``app/web/router.py::_compute_can_chat`` uses ``has_explicit_grant``,
deliberately NOT ``can_access`` — admin god-mode does not reveal chat). A
fresh instance with ``chat.enabled: true`` and no grant ships with a fully
working, fully invisible chat backend: only a hand-typed ``/chat`` URL
works, even for admins.

``app.chat.grant_seed.seed_everyone_chat_grant`` seeds ``(Everyone, chat,
chat)`` once, on the instance's genuine first boot. ``resource_grants``
carries no provenance column (unlike ``user_group_members.source``), so
"never seeded" and "an admin revoked it" are indistinguishable from the
grants table alone — the caller (``app.main`` lifespan) gates the seed on
whether the ``Everyone`` system group already existed *before* this boot's
group-seeding step ran, which can only be false once in an instance's
lifetime (system groups are never deleted). These tests exercise the
function directly on both backends via the repository factory.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def _env(state_backend, tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for sub in ("extracts", "analytics", "state", "notifications"):
        (tmp_path / sub).mkdir(exist_ok=True)
    if state_backend == "duckdb":
        from src.db import close_system_db, get_system_db

        close_system_db()
        get_system_db()  # triggers _ensure_schema + _seed_system_groups
    return state_backend


def _everyone_group_id() -> str:
    from src.db import SYSTEM_EVERYONE_GROUP
    from src.repositories import user_groups_repo

    grp = user_groups_repo().get_by_name(SYSTEM_EVERYONE_GROUP)
    assert grp is not None, "Everyone system group must be pre-seeded by the _env fixture"
    return grp["id"]


def test_seeds_grant_on_fresh_instance_when_chat_enabled(_env):
    from app.chat.grant_seed import seed_everyone_chat_grant
    from src.repositories import resource_grants_repo

    everyone_id = _everyone_group_id()

    seeded = seed_everyone_chat_grant(chat_enabled=True)

    assert seeded is True, f"[{_env}] must report having seeded the grant"
    assert resource_grants_repo().has_grant([everyone_id], "chat", "chat"), (
        f"[{_env}] Everyone must hold the (chat, chat) grant after a fresh-boot seed"
    )


def test_does_not_seed_when_chat_disabled(_env):
    from app.chat.grant_seed import seed_everyone_chat_grant
    from src.repositories import resource_grants_repo

    everyone_id = _everyone_group_id()

    seeded = seed_everyone_chat_grant(chat_enabled=False)

    assert seeded is False, f"[{_env}] must not seed when chat is disabled"
    assert not resource_grants_repo().has_grant([everyone_id], "chat", "chat")


def test_does_not_reseed_after_admin_revokes_it(_env):
    """Once the grant has been seeded, an admin who revokes it must never
    see it silently reappear on the next restart — the marker file records
    that we already seeded, which `resource_grants` alone cannot express."""
    from app.chat.grant_seed import seed_everyone_chat_grant
    from src.repositories import resource_grants_repo

    everyone_id = _everyone_group_id()

    # Seed once (writes the marker), then have an admin revoke the grant.
    assert seed_everyone_chat_grant(chat_enabled=True) is True
    grants = resource_grants_repo().list_all(resource_type="chat", group_id=everyone_id)
    resource_grants_repo().delete(grants[0]["id"])
    assert not resource_grants_repo().has_grant([everyone_id], "chat", "chat")

    # Next boot: chat is still enabled and no chat grant exists, but the
    # marker says we already seeded — so this must NOT bring it back.
    seeded = seed_everyone_chat_grant(chat_enabled=True)

    assert seeded is False, f"[{_env}] must not re-add a grant an admin deliberately revoked"
    assert not resource_grants_repo().has_grant([everyone_id], "chat", "chat")


def test_idempotent_double_seed_does_not_duplicate(_env):
    from app.chat.grant_seed import seed_everyone_chat_grant
    from src.repositories import resource_grants_repo

    everyone_id = _everyone_group_id()

    seed_everyone_chat_grant(chat_enabled=True)
    seed_everyone_chat_grant(chat_enabled=True)

    grants = resource_grants_repo().list_all(resource_type="chat", group_id=everyone_id)
    assert len(grants) == 1, f"[{_env}] repeated seed calls must not duplicate the grant row"
