"""Idempotency + safety contract tests for scripts/seed_e2e_user.py."""

from __future__ import annotations

import sys

import pytest

# scripts/ is not a Python package; load by path
import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SEED_PATH = REPO_ROOT / "scripts" / "seed_e2e_user.py"


def _load_seed_module():
    spec = importlib.util.spec_from_file_location("seed_e2e_user", SEED_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def seed_module():
    return _load_seed_module()


def test_seed_creates_admin_user_on_fresh_db(e2e_env, seed_module):
    """Fresh DB -> user is created with password hash + Admin membership."""
    from src.db import SYSTEM_ADMIN_GROUP, get_system_db
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.users import UserRepository

    seed_module.seed()

    conn = get_system_db()
    user = UserRepository(conn).get_by_email(seed_module.E2E_USER_EMAIL)
    assert user is not None
    assert user["password_hash"], "password_hash must be set"

    admin_gid = conn.execute(
        "SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]
    ).fetchone()[0]

    member_ids = [
        m["id"]
        for m in UserGroupMembersRepository(conn).list_members_for_group(admin_gid)
    ]
    assert user["id"] in member_ids
    conn.close()


def test_seed_is_idempotent(e2e_env, seed_module):
    """Running seed twice does not duplicate the user or fail."""
    from src.db import SYSTEM_ADMIN_GROUP, get_system_db
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.users import UserRepository

    seed_module.seed()
    seed_module.seed()  # must not raise, must not duplicate

    conn = get_system_db()
    matches = conn.execute(
        "SELECT COUNT(*) FROM users WHERE email = ?",
        [seed_module.E2E_USER_EMAIL],
    ).fetchone()[0]
    assert matches == 1

    admin_gid = conn.execute(
        "SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]
    ).fetchone()[0]
    members = UserGroupMembersRepository(conn).list_members_for_group(admin_gid)
    e2e_member_rows = [m for m in members if m["id"] == seed_module.E2E_USER_ID]
    assert len(e2e_member_rows) == 1
    conn.close()


def test_seed_refuses_when_admin_group_missing(e2e_env, seed_module):
    """If the Admin system group is absent, seed exits 1 -- never an orphan user."""
    from src.db import SYSTEM_ADMIN_GROUP, get_system_db
    from src.repositories.users import UserRepository

    # Drop the Admin group to simulate half-init DB
    conn = get_system_db()
    conn.execute("DELETE FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP])
    conn.close()

    with pytest.raises(SystemExit) as excinfo:
        seed_module.seed()
    assert excinfo.value.code == 1

    # And no orphan user was created.
    conn = get_system_db()
    user = UserRepository(conn).get_by_email(seed_module.E2E_USER_EMAIL)
    assert user is None
    conn.close()
