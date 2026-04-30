"""Tests for src/rbac.py — table access via resource_grants (v19+).

``can_access_table`` and ``get_accessible_tables`` are thin wrappers over
``app.auth.access.can_access`` / ``is_user_admin``. Admin group members see
everything; non-admin users see only tables with a matching
``resource_grants(group, "table", id)`` row via any of their groups.
"""

from __future__ import annotations

import uuid

import pytest


@pytest.fixture
def setup_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.db import SYSTEM_ADMIN_GROUP, get_system_db
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.users import UserRepository

    conn = get_system_db()
    UserRepository(conn).create(id="admin1", email="admin@test.com", name="Admin")
    UserRepository(conn).create(id="user1", email="user@test.com", name="User")

    admin_gid = conn.execute(
        "SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]
    ).fetchone()[0]
    UserGroupMembersRepository(conn).add_member("admin1", admin_gid, source="system_seed")

    # Custom group + grant: user1 ∈ analysts, analysts can see "orders"
    analysts = UserGroupsRepository(conn).create(
        name="analysts", description="test group", created_by="test",
    )
    UserGroupMembersRepository(conn).add_member(
        "user1", analysts["id"], source="admin", added_by="test",
    )
    conn.execute(
        "INSERT INTO table_registry (id, name) VALUES (?, ?)",
        ["orders", "orders"],
    )
    conn.execute(
        "INSERT INTO table_registry (id, name) VALUES (?, ?)",
        ["salaries", "salaries"],
    )
    conn.execute(
        """INSERT INTO resource_grants (id, group_id, resource_type, resource_id)
           VALUES (?, ?, 'table', 'orders')""",
        [str(uuid.uuid4()), analysts["id"]],
    )

    conn.close()
    yield


class TestCanAccessTable:
    """Admin shortcut + per-(group, table) grants. No is_public, no
    dataset_permissions, no bucket wildcards — explicit grants only."""

    def test_admin_sees_every_table(self, setup_db):
        from src.db import get_system_db
        from src.rbac import can_access_table
        conn = get_system_db()
        try:
            admin = {"id": "admin1"}
            assert can_access_table(admin, "orders", conn) is True
            assert can_access_table(admin, "salaries", conn) is True
            # Admin can even ask about tables that don't exist.
            assert can_access_table(admin, "nonexistent", conn) is True
        finally:
            conn.close()

    def test_non_admin_sees_only_granted_tables(self, setup_db):
        from src.db import get_system_db
        from src.rbac import can_access_table
        conn = get_system_db()
        try:
            user = {"id": "user1"}
            # user1's group "analysts" was granted resource_id='orders'
            assert can_access_table(user, "orders", conn) is True
            # No grant for 'salaries' → denied
            assert can_access_table(user, "salaries", conn) is False
        finally:
            conn.close()

    def test_no_implicit_public_access(self, setup_db):
        """Pre-v19 a freshly registered table was implicitly public via
        ``is_public DEFAULT true``. v19 removes the column — every
        non-admin access requires an explicit grant. Verify by
        registering a fresh table and asserting denial."""
        from src.db import get_system_db
        from src.rbac import can_access_table
        conn = get_system_db()
        try:
            conn.execute(
                "INSERT INTO table_registry (id, name) VALUES (?, ?)",
                ["fresh_table", "fresh_table"],
            )
            user = {"id": "user1"}
            assert can_access_table(user, "fresh_table", conn) is False
        finally:
            conn.close()

    def test_unknown_user_id_denied(self, setup_db):
        from src.db import get_system_db
        from src.rbac import can_access_table
        conn = get_system_db()
        try:
            assert can_access_table({"id": "ghost"}, "orders", conn) is False
            # No id at all → denied (defensive default).
            assert can_access_table({}, "orders", conn) is False
        finally:
            conn.close()


class TestGetAccessibleTables:
    """Admin returns None (= "all"); non-admin returns the grant list."""

    def test_admin_returns_none(self, setup_db):
        from src.db import get_system_db
        from src.rbac import get_accessible_tables
        conn = get_system_db()
        try:
            assert get_accessible_tables({"id": "admin1"}, conn) is None
        finally:
            conn.close()

    def test_non_admin_returns_grant_list(self, setup_db):
        from src.db import get_system_db
        from src.rbac import get_accessible_tables
        conn = get_system_db()
        try:
            tables = get_accessible_tables({"id": "user1"}, conn)
            assert tables == ["orders"]
        finally:
            conn.close()

    def test_user_with_no_grants_returns_empty(self, setup_db):
        from src.db import SYSTEM_EVERYONE_GROUP, get_system_db
        from src.repositories.user_group_members import UserGroupMembersRepository
        from src.repositories.users import UserRepository
        from src.rbac import get_accessible_tables
        conn = get_system_db()
        try:
            UserRepository(conn).create(id="loner", email="loner@test.com", name="L")
            # Membership in Everyone alone (no grants on it) → empty list.
            everyone = conn.execute(
                "SELECT id FROM user_groups WHERE name = ?", [SYSTEM_EVERYONE_GROUP]
            ).fetchone()
            if everyone:
                UserGroupMembersRepository(conn).add_member(
                    "loner", everyone[0], source="system_seed",
                )
            assert get_accessible_tables({"id": "loner"}, conn) == []
        finally:
            conn.close()
