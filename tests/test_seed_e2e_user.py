"""Idempotency + safety contract tests for scripts/seed_e2e_user.py.

Post-PG cutover: the script talks to the repository factory; the tests
mirror it. ``e2e_env`` no longer materialises a DuckDB; the shared dev
Postgres serves the writes, and ``_truncate_pg_app_state`` between
tests keeps state clean.
"""

from __future__ import annotations

import sys

import pytest

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SEED_PATH = REPO_ROOT / "scripts" / "seed_e2e_user.py"


def _load_seed_module():
    spec = importlib.util.spec_from_file_location("seed_e2e_user", SEED_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _clean_users_and_groups():
    """Per-test wipe of users/groups so the test order doesn't matter.

    The seed under test writes into the shared dev/test PG; without a
    per-test reset, a prior test leaves ``e2e@example.com`` (or wipes
    the Admin group) and the next test starts from contaminated state.
    ``_truncate_pg_app_state`` keeps the Admin/Everyone rows in
    ``user_groups`` (its preserve list); we re-seed them defensively
    here in case the prior test was ``test_seed_refuses_when_admin_group_missing``
    which DELETEs Admin to simulate the half-init DB.
    """
    import uuid as _uuid
    import sqlalchemy as sa
    from tests.conftest import _truncate_pg_app_state
    from src.db import SYSTEM_ADMIN_GROUP, SYSTEM_EVERYONE_GROUP
    from src.db_pg import get_engine

    def _restore_system_groups() -> None:
        with get_engine().begin() as conn:
            for name, desc in (
                (SYSTEM_ADMIN_GROUP, "System: full access"),
                (SYSTEM_EVERYONE_GROUP, "System: default group"),
            ):
                conn.execute(
                    sa.text(
                        "INSERT INTO user_groups (id, name, description, is_system, created_by) "
                        "VALUES (:id, :name, :desc, TRUE, 'system:seed') "
                        "ON CONFLICT (name) DO UPDATE SET is_system = TRUE"
                    ),
                    {"id": _uuid.uuid4().hex, "name": name, "desc": desc},
                )

    _truncate_pg_app_state()
    _restore_system_groups()
    yield
    # Also re-seed at teardown — ``test_seed_refuses_when_admin_group_missing``
    # explicitly DELETEs the Admin row; without this fix-up, the next test
    # (whether in this file or any other on the same xdist worker) inherits
    # a half-init DB and fails the admin gate.
    _restore_system_groups()


@pytest.fixture
def seed_module():
    return _load_seed_module()


@pytest.fixture
def e2e_seed_env(e2e_env, monkeypatch):
    """e2e_env + AGNES_E2E_SEED=1 opt-in. Mirrors the CI workflow.

    The seed script refuses to run without this env var (defence-in-depth so
    a stray ``docker exec`` on a production image can't mint Admin users);
    every happy-path test needs the opt-in set.
    """
    monkeypatch.setenv("AGNES_E2E_SEED", "1")
    return e2e_env


def test_seed_refuses_without_opt_in_env(e2e_env, seed_module):
    """Without AGNES_E2E_SEED=1 -> SystemExit(1), no user created."""
    from src.repositories import users_repo

    with pytest.raises(SystemExit) as excinfo:
        seed_module.seed()
    assert excinfo.value.code == 1

    user = users_repo().get_by_email(seed_module.E2E_USER_EMAIL)
    assert user is None, "no user should be created when opt-in is missing"


def test_seed_creates_admin_user_on_fresh_db(e2e_seed_env, seed_module):
    """Fresh DB -> user is created with password hash + Admin membership."""
    from src.db import SYSTEM_ADMIN_GROUP
    from src.repositories import (
        user_group_members_repo,
        user_groups_repo,
        users_repo,
    )

    seed_module.seed()

    user = users_repo().get_by_email(seed_module.E2E_USER_EMAIL)
    assert user is not None
    assert user["password_hash"], "password_hash must be set"

    admin = user_groups_repo().get_by_name(SYSTEM_ADMIN_GROUP)
    assert admin is not None
    members = user_group_members_repo().list_members_for_group(admin["id"])
    member_ids = [m["id"] for m in members]
    assert user["id"] in member_ids


def test_seed_is_idempotent(e2e_seed_env, seed_module):
    """Running seed twice does not duplicate the user or fail."""
    import sqlalchemy as sa
    from src.db import SYSTEM_ADMIN_GROUP
    from src.db_pg import get_engine
    from src.repositories import user_group_members_repo, user_groups_repo

    seed_module.seed()
    seed_module.seed()  # must not raise, must not duplicate

    with get_engine().connect() as conn:
        matches = conn.execute(
            sa.text("SELECT COUNT(*) FROM users WHERE email = :e"),
            {"e": seed_module.E2E_USER_EMAIL},
        ).scalar()
    assert matches == 1

    admin = user_groups_repo().get_by_name(SYSTEM_ADMIN_GROUP)
    members = user_group_members_repo().list_members_for_group(admin["id"])
    e2e_member_rows = [m for m in members if m["id"] == seed_module.E2E_USER_ID]
    assert len(e2e_member_rows) == 1


def test_seed_refuses_when_admin_group_missing(e2e_seed_env, seed_module):
    """If the Admin system group is absent, seed exits 1 -- never an orphan user."""
    import sqlalchemy as sa
    from src.db import SYSTEM_ADMIN_GROUP
    from src.db_pg import get_engine
    from src.repositories import users_repo

    # Drop the Admin group to simulate half-init DB. PG ``DELETE``
    # doesn't take a CASCADE keyword (that's a DDL-only modifier on
    # DROP TABLE / TRUNCATE); wipe dependent membership rows first
    # to satisfy the FK, then the parent.
    with get_engine().begin() as conn:
        admin_id = conn.execute(
            sa.text("SELECT id FROM user_groups WHERE name = :n"),
            {"n": SYSTEM_ADMIN_GROUP},
        ).scalar()
        if admin_id is not None:
            conn.execute(
                sa.text("DELETE FROM user_group_members WHERE group_id = :g"),
                {"g": admin_id},
            )
            conn.execute(
                sa.text("DELETE FROM resource_grants WHERE group_id = :g"),
                {"g": admin_id},
            )
            conn.execute(
                sa.text("DELETE FROM user_groups WHERE id = :g"),
                {"g": admin_id},
            )

    with pytest.raises(SystemExit) as excinfo:
        seed_module.seed()
    assert excinfo.value.code == 1

    user = users_repo().get_by_email(seed_module.E2E_USER_EMAIL)
    assert user is None
