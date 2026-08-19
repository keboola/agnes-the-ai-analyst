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
grants table alone; a marker file on the state volume records that the
question is settled.

Two gates, because the marker is younger than the instances it reasons
about: the marker itself, and — on the first boot after upgrading to the
build that introduced the marker, where a long-running instance has none
either — the presence of ANY ``(chat, chat)`` grant, which proves a human
already decided who gets chat. These tests exercise the function directly
on both backends via the repository factory.
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


def test_does_not_widen_an_existing_narrow_grant_on_upgrade(_env):
    """The marker cannot tell a fresh deploy from a long-running instance
    booting for the first time on the build that introduced the marker —
    neither has one. Seeding there would take an instance whose admin gave
    chat to a single group and hand it to every user (``Everyone`` is
    auto-membership). A pre-existing ``(chat, chat)`` grant is the signal
    that a human already decided, so nothing is seeded."""
    from app.chat.grant_seed import MARKER_NAME, seed_everyone_chat_grant
    from app.secrets import _state_dir
    from src.repositories import resource_grants_repo, user_groups_repo

    everyone_id = _everyone_group_id()

    # An admin's deliberately narrow rollout: chat granted to Analysts only.
    analysts = user_groups_repo().create(name="Analysts", created_by="admin@example.com")
    resource_grants_repo().ensure_grant(
        group_id=analysts["id"],
        resource_type="chat",
        resource_id="chat",
        assigned_by="admin@example.com",
    )
    # First boot on the build that introduced the marker: none exists yet.
    assert not (_state_dir() / MARKER_NAME).exists()

    seeded = seed_everyone_chat_grant(chat_enabled=True)

    assert seeded is False, f"[{_env}] must not seed over an admin-configured chat grant"
    assert not resource_grants_repo().has_grant([everyone_id], "chat", "chat"), (
        f"[{_env}] upgrading must never widen a narrow chat rollout to Everyone"
    )
    assert resource_grants_repo().has_grant([analysts["id"]], "chat", "chat"), (
        f"[{_env}] the admin's own grant must survive untouched"
    )
    # The question is settled from now on, so later boots stop re-checking.
    assert (_state_dir() / MARKER_NAME).exists(), f"[{_env}] the skip must be recorded, like the seed is"


def test_idempotent_double_seed_does_not_duplicate(_env):
    from app.chat.grant_seed import seed_everyone_chat_grant
    from src.repositories import resource_grants_repo

    everyone_id = _everyone_group_id()

    seed_everyone_chat_grant(chat_enabled=True)
    seed_everyone_chat_grant(chat_enabled=True)

    grants = resource_grants_repo().list_all(resource_type="chat", group_id=everyone_id)
    assert len(grants) == 1, f"[{_env}] repeated seed calls must not duplicate the grant row"
