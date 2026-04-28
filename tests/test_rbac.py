"""Tests for src/rbac.py — dataset access checks (v12).

The v9 hierarchy/role helpers (`Role`, `has_role`, `is_admin`, etc.) are
gone; admin authorization is now ``app.auth.access.is_user_admin``. What
remains in ``src.rbac`` is dataset-level table access.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def setup_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.db import SYSTEM_ADMIN_GROUP, get_system_db
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.users import UserRepository

    conn = get_system_db()
    repo = UserRepository(conn)
    repo.create(id="admin1", email="admin@test.com", name="Admin")
    repo.create(id="user1", email="user@test.com", name="User")

    admin_gid = conn.execute(
        "SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]
    ).fetchone()[0]
    UserGroupMembersRepository(conn).add_member("admin1", admin_gid, source="system_seed")

    conn.close()
    yield


class TestIsUserAdmin:
    def test_admin_membership_makes_user_admin(self, setup_db):
        from src.rbac import _is_admin_user_dict
        from src.repositories.users import UserRepository
        from src.db import get_system_db
        conn = get_system_db()
        try:
            admin = UserRepository(conn).get_by_email("admin@test.com")
            assert _is_admin_user_dict(admin, conn=conn) is True
        finally:
            conn.close()

    def test_non_admin_user(self, setup_db):
        from src.rbac import _is_admin_user_dict
        from src.repositories.users import UserRepository
        from src.db import get_system_db
        conn = get_system_db()
        try:
            user = UserRepository(conn).get_by_email("user@test.com")
            assert _is_admin_user_dict(user, conn=conn) is False
        finally:
            conn.close()


class TestHasDatasetAccess:
    def test_admin_has_all_datasets(self, setup_db):
        from src.rbac import has_dataset_access
        assert has_dataset_access("admin@test.com", "any-dataset")

    def test_unknown_user_has_no_access(self, setup_db):
        from src.rbac import has_dataset_access
        assert not has_dataset_access("nobody@test.com", "any-dataset")

    def test_user_without_explicit_grant_has_no_access(self, setup_db):
        from src.rbac import has_dataset_access
        assert not has_dataset_access("user@test.com", "private-data")
