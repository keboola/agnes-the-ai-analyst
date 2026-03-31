"""Tests for src/rbac.py — role-based access control."""

import os
import pytest


@pytest.fixture
def setup_db(tmp_path):
    os.environ["DATA_DIR"] = str(tmp_path)
    from src.db import get_system_db
    from src.repositories.users import UserRepository

    conn = get_system_db()
    repo = UserRepository(conn)
    repo.create(id="admin1", email="admin@test.com", name="Admin", role="admin")
    repo.create(id="analyst1", email="analyst@test.com", name="Analyst", role="analyst")
    repo.create(id="km1", email="km@test.com", name="KM Admin", role="km_admin")
    repo.create(id="viewer1", email="viewer@test.com", name="Viewer", role="viewer")
    conn.close()
    yield


class TestGetUserRole:
    def test_admin(self, setup_db):
        from src.rbac import get_user_role, Role
        assert get_user_role("admin@test.com") == Role.ADMIN

    def test_analyst(self, setup_db):
        from src.rbac import get_user_role, Role
        assert get_user_role("analyst@test.com") == Role.ANALYST

    def test_unknown_user(self, setup_db):
        from src.rbac import get_user_role, Role
        assert get_user_role("nobody@test.com") == Role.VIEWER


class TestHasRole:
    def test_admin_has_all_roles(self, setup_db):
        from src.rbac import has_role, Role
        assert has_role("admin@test.com", Role.VIEWER)
        assert has_role("admin@test.com", Role.ANALYST)
        assert has_role("admin@test.com", Role.KM_ADMIN)
        assert has_role("admin@test.com", Role.ADMIN)

    def test_analyst_cant_admin(self, setup_db):
        from src.rbac import has_role, Role
        assert has_role("analyst@test.com", Role.ANALYST)
        assert not has_role("analyst@test.com", Role.ADMIN)

    def test_viewer_is_minimal(self, setup_db):
        from src.rbac import has_role, Role
        assert has_role("viewer@test.com", Role.VIEWER)
        assert not has_role("viewer@test.com", Role.ANALYST)


class TestConvenienceFunctions:
    def test_is_admin(self, setup_db):
        from src.rbac import is_admin
        assert is_admin("admin@test.com")
        assert not is_admin("analyst@test.com")

    def test_is_km_admin(self, setup_db):
        from src.rbac import is_km_admin
        assert is_km_admin("km@test.com")
        assert is_km_admin("admin@test.com")  # admin >= km_admin
        assert not is_km_admin("analyst@test.com")

    def test_is_analyst(self, setup_db):
        from src.rbac import is_analyst
        assert is_analyst("analyst@test.com")
        assert is_analyst("admin@test.com")
        assert not is_analyst("viewer@test.com")


class TestSetUserRole:
    def test_set_role(self, setup_db):
        from src.rbac import set_user_role, get_user_role, Role
        assert get_user_role("viewer@test.com") == Role.VIEWER
        assert set_user_role("viewer@test.com", Role.ADMIN)
        assert get_user_role("viewer@test.com") == Role.ADMIN

    def test_set_role_nonexistent(self, setup_db):
        from src.rbac import set_user_role, Role
        assert not set_user_role("nobody@test.com", Role.ADMIN)
