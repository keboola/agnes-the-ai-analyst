"""Tests for #4 — uploaded CSV/Excel → owner-private queryable table.

A tabular upload becomes a ``table_registry`` row with
``source_type='collection'`` and ``bucket=<corpus_id>`` (see
``src/ingest/tabular.py``). Its table access must inherit the owning
collection's access — owner OR group share — rather than the data-package
stack. These tests pin that behaviour in ``src.rbac.can_access_table`` /
``get_accessible_tables``.
"""

from __future__ import annotations

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
    UserRepository(conn).create(id="owner1", email="owner@test.com", name="Owner")
    UserRepository(conn).create(id="other1", email="other@test.com", name="Other")

    admin_gid = conn.execute(
        "SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]
    ).fetchone()[0]
    UserGroupMembersRepository(conn).add_member("admin1", admin_gid, source="system_seed")

    # other1 ∈ analysts (a non-admin group we can later grant the collection to).
    analysts = UserGroupsRepository(conn).create(
        name="analysts", description="test group", created_by="test"
    )
    UserGroupMembersRepository(conn).add_member(
        "other1", analysts["id"], source="admin", added_by="test"
    )

    # A collection owned by owner1, plus its derived tabular table.
    from src.repositories import file_corpora_repo, table_registry_repo

    corpus_id = file_corpora_repo().create(
        name="My Upload", slug="my-upload", description=None, created_by="owner1"
    )
    table_id = f"collection_{corpus_id}_sales_abcd1234"
    table_registry_repo().register(
        id=table_id,
        name="sales",
        registered_by="owner1",
        source_type="collection",
        bucket=corpus_id,
    )

    return {
        "conn": conn,
        "corpus_id": corpus_id,
        "table_id": table_id,
        "analysts_gid": analysts["id"],
    }


def _u(uid):
    return {"id": uid}


def test_owner_can_access_derived_table(setup_db):
    from src.rbac import can_access_table

    assert can_access_table(_u("owner1"), setup_db["table_id"], setup_db["conn"]) is True


def test_other_cannot_access_derived_table(setup_db):
    from src.rbac import can_access_table

    assert can_access_table(_u("other1"), setup_db["table_id"], setup_db["conn"]) is False


def test_admin_can_access_derived_table(setup_db):
    from src.rbac import can_access_table

    assert can_access_table(_u("admin1"), setup_db["table_id"], setup_db["conn"]) is True


def test_owner_sees_derived_table_in_accessible_list(setup_db):
    from src.rbac import get_accessible_tables

    tables = get_accessible_tables(_u("owner1"), setup_db["conn"])
    assert setup_db["table_id"] in tables


def test_other_does_not_see_derived_table_in_accessible_list(setup_db):
    from src.rbac import get_accessible_tables

    tables = get_accessible_tables(_u("other1"), setup_db["conn"])
    assert setup_db["table_id"] not in tables


def test_sharing_collection_grants_access(setup_db):
    """Granting the owning COLLECTION to other1's group extends table access."""
    from app.resource_types import ResourceType
    from src.rbac import can_access_table, get_accessible_tables
    from src.repositories.resource_grants import ResourceGrantsRepository

    ResourceGrantsRepository(setup_db["conn"]).create(
        group_id=setup_db["analysts_gid"],
        resource_type=ResourceType.COLLECTION.value,
        resource_id=setup_db["corpus_id"],
        assigned_by="admin1",
    )

    assert can_access_table(_u("other1"), setup_db["table_id"], setup_db["conn"]) is True
    assert setup_db["table_id"] in get_accessible_tables(_u("other1"), setup_db["conn"])
