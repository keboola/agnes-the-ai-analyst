"""Parity test for system-group seeding + seed-admin membership.

A fresh instance must end up with the ``Admin`` and ``Everyone`` system groups
and a seed admin who is actually a *member* of ``Admin`` (that membership is
what grants admin access). On DuckDB ``src.db._seed_system_groups`` handles the
groups on connect, but it never runs on Postgres — nothing seeded the groups
there, and the lifespan seed-admin path then looked the Admin group up off a
raw DuckDB connection, so the membership it wrote referenced a DuckDB-only
group id that does not exist on Postgres → the seed admin had no admin access.

The fix seeds the groups through the factory (``ensure_system``) and looks them
up through the factory (``get_by_name``). These tests exercise that exact
sequence and assert the seed admin resolves as an admin on DuckDB AND Postgres.
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
        get_system_db()
    return state_backend


def _seed_like_lifespan(seed_admin_id: str, seed_email: str) -> None:
    """Replay the lifespan seed sequence through the factory (the fixed path)."""
    from src.db import _SYSTEM_GROUPS_SEED, SYSTEM_ADMIN_GROUP, SYSTEM_EVERYONE_GROUP
    from src.repositories import (
        user_group_members_repo,
        user_groups_repo,
        users_repo,
    )

    for name, desc in _SYSTEM_GROUPS_SEED:
        user_groups_repo().ensure_system(name, desc)

    users_repo().create(id=seed_admin_id, email=seed_email, name="Admin")

    for group_name in (SYSTEM_ADMIN_GROUP, SYSTEM_EVERYONE_GROUP):
        grp = user_groups_repo().get_by_name(group_name)
        assert grp is not None, f"system group {group_name!r} not seeded"
        user_group_members_repo().add_member(
            user_id=seed_admin_id,
            group_id=grp["id"],
            source="system_seed",
            added_by="test",
        )


def test_system_groups_seeded_on_both_backends(_env):
    from src.db import SYSTEM_ADMIN_GROUP, SYSTEM_EVERYONE_GROUP
    from src.repositories import user_groups_repo

    _seed_like_lifespan("seed_admin", "seed@example.com")

    names = {g["name"] for g in user_groups_repo().list_all()}
    assert {SYSTEM_ADMIN_GROUP, SYSTEM_EVERYONE_GROUP} <= names, f"[{_env}] system groups missing after seed: {names}"


def test_seed_admin_has_admin_access_on_both_backends(_env):
    from app.auth.access import is_user_admin

    _seed_like_lifespan("seed_admin", "seed@example.com")

    assert is_user_admin("seed_admin") is True, (
        f"[{_env}] seed admin lacks admin access — the Admin-group membership "
        f"did not resolve on this backend (the pre-fix raw-DuckDB group lookup "
        f"wrote a group id absent from Postgres)."
    )


def test_everyone_membership_resolves_on_both_backends(_env):
    from src.db import SYSTEM_EVERYONE_GROUP
    from app.auth.access import _user_group_ids
    from src.repositories import user_groups_repo

    _seed_like_lifespan("seed_admin", "seed@example.com")

    everyone = user_groups_repo().get_by_name(SYSTEM_EVERYONE_GROUP)
    assert everyone is not None
    assert everyone["id"] in _user_group_ids("seed_admin"), (
        f"[{_env}] seed admin not resolved into Everyone — Everyone-scoped grants would not surface for them."
    )


def test_ensure_everyone_membership_grants_on_both_backends(_env, monkeypatch):
    """Issue #748: ``app.auth.group_sync.ensure_everyone_membership`` routes
    through the ``src.repositories`` factory pair exclusively, so a
    creation-time grant must resolve identically on DuckDB and Postgres —
    same pattern as the seed-admin bug this file otherwise covers (a raw
    DuckDB group lookup writing an id absent from Postgres)."""
    monkeypatch.delenv("AGNES_GROUP_EVERYONE_EMAIL", raising=False)
    from src.db import SYSTEM_EVERYONE_GROUP
    from src.repositories import user_group_members_repo, user_groups_repo, users_repo
    from app.auth.group_sync import ensure_everyone_membership

    _seed_like_lifespan("seed_admin", "seed@example.com")
    users_repo().create(id="grant-check", email="grant-check@example.com", name="U")

    result = ensure_everyone_membership("grant-check", added_by="test:parity")
    assert result is True, f"[{_env}] ensure_everyone_membership returned False unexpectedly"

    everyone = user_groups_repo().get_by_name(SYSTEM_EVERYONE_GROUP)
    assert everyone is not None, f"[{_env}] Everyone group not resolvable"
    rows = user_group_members_repo().list_groups_with_meta_for_user("grant-check")
    matching = [r for r in rows if r["group_id"] == everyone["id"]]
    assert len(matching) == 1, f"[{_env}] expected exactly one Everyone row, got {matching}"
    assert matching[0]["source"] == "system_seed"


def test_ensure_everyone_membership_env_set_noop_on_both_backends(_env, monkeypatch):
    """Dual-mode: env set → no local grant written on either backend."""
    monkeypatch.setenv("AGNES_GROUP_EVERYONE_EMAIL", "everyone@workspace.test")
    from src.repositories import user_group_members_repo, users_repo
    from app.auth.group_sync import ensure_everyone_membership

    _seed_like_lifespan("seed_admin", "seed@example.com")
    users_repo().create(id="grant-check-mapped", email="grant-check-mapped@example.com", name="U")

    result = ensure_everyone_membership("grant-check-mapped", added_by="test:parity")
    assert result is False, f"[{_env}] must no-op when AGNES_GROUP_EVERYONE_EMAIL is set"

    rows = user_group_members_repo().list_groups_with_meta_for_user("grant-check-mapped")
    assert rows == [], f"[{_env}] no membership rows expected, got {rows}"


def test_ensure_system_creates_absent_group_both_backends(_env):
    """The fresh-PG bug was that nothing *creates* the system groups on Postgres
    (Admin/Everyone are protected from deletion + the fixtures pre-seed them, so
    they can't be removed to simulate empty). Exercise the create path the
    lifespan relies on directly: ``ensure_system`` on a name that doesn't exist
    yet must CREATE it (not just promote) as a system group — on both backends."""
    from src.repositories import user_groups_repo

    repo = user_groups_repo()
    assert repo.get_by_name("ProbeSysGroup") is None, f"[{_env}] probe group pre-exists"

    repo.ensure_system("ProbeSysGroup", "probe system group")

    grp = repo.get_by_name("ProbeSysGroup")
    assert grp is not None, f"[{_env}] ensure_system did not create the absent group"
    assert grp["is_system"] is True, f"[{_env}] created group is not is_system"
