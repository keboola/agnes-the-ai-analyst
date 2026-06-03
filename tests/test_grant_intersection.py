import pytest

from src.db import SYSTEM_ADMIN_GROUP


@pytest.fixture
def conn(e2e_env):
    # e2e_env (tests/conftest.py) points DATA_DIR at tmp + sets a 32-char
    # JWT_SECRET_KEY. get_system_db() migrates a fresh DB to SCHEMA_VERSION.
    from src.db import get_system_db
    from src.repositories.users import UserRepository
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    c = get_system_db()
    users = UserRepository(c)
    users.create(id="ua", email="a@example.com", name="A")
    users.create(id="ub", email="b@example.com", name="B")
    users.create(id="uadm", email="adm@example.com", name="Adm")
    admin_gid = c.execute(
        "SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]
    ).fetchone()[0]
    UserGroupMembersRepository(c).add_member("uadm", admin_gid, source="system_seed")
    # Register tables in table_registry so data_package_tables FK constraint passes
    from src.repositories.table_registry import TableRegistryRepository
    reg = TableRegistryRepository(c)
    for tid in ("t1", "t2", "t3"):
        reg.register(id=tid, name=tid, source_type="keboola")
    # ua holds packages wrapping t1 + t2; ub holds packages wrapping t2 + t3.
    from tests.conftest import grant_table_via_package
    grant_table_via_package(c, "t1", "ua", group_name="g-a")
    grant_table_via_package(c, "t2", "ua", group_name="g-a")
    grant_table_via_package(c, "t2", "ub", group_name="g-b")
    grant_table_via_package(c, "t3", "ub", group_name="g-b")
    yield c
    c.close()


def _pkg_ids(conn, group_name):
    from src.repositories.user_groups import UserGroupsRepository
    gid = UserGroupsRepository(conn).get_by_name(group_name)["id"]
    rows = conn.execute(
        "SELECT resource_id FROM resource_grants WHERE group_id = ? AND resource_type = 'data_package'",
        [gid],
    ).fetchall()
    return frozenset(r[0] for r in rows)


def test_allowed_ids_excludes_admin_short_circuit(conn):
    from app.auth.access import _allowed_ids_for_user
    # admin user has NO data_package grants -> empty set, not "everything"
    assert _allowed_ids_for_user("uadm", "data_package", conn) == frozenset()
    assert _allowed_ids_for_user("ua", "data_package", conn) == _pkg_ids(conn, "g-a")


def test_intersection_two_non_admins_overlap(conn):
    from src.grant_intersection import compute_grant_intersection
    inter = compute_grant_intersection(["a@example.com", "b@example.com"], conn)
    # only the t2-wrapping package is granted to BOTH g-a and g-b? No — each
    # call to grant_table_via_package makes a per-table package, so the
    # overlap is empty unless the same package id is shared. Assert the
    # intersection of the two distinct package sets.
    shared = _pkg_ids(conn, "g-a") & _pkg_ids(conn, "g-b")
    assert inter.get("data_package", frozenset()) == shared


def test_intersection_admin_plus_nonadmin_is_nonadmin_set(conn):
    from src.grant_intersection import compute_grant_intersection
    # give admin the SAME packages ua holds, so intersection == ua's set
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.resource_grants import ResourceGrantsRepository
    admin_gid = conn.execute(
        "SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]
    ).fetchone()[0]
    grants = ResourceGrantsRepository(conn)
    for pid in _pkg_ids(conn, "g-a"):
        if not grants.has_grant([admin_gid], "data_package", pid):
            grants.create(group_id=admin_gid, resource_type="data_package",
                          resource_id=pid, assigned_by="test", requirement="required")
    inter = compute_grant_intersection(["adm@example.com", "a@example.com"], conn)
    assert inter.get("data_package", frozenset()) == _pkg_ids(conn, "g-a")


def test_intersection_grantless_participant_denies_all(conn):
    from src.grant_intersection import compute_grant_intersection
    from src.repositories.users import UserRepository
    UserRepository(conn).create(id="uc", email="c@example.com", name="C")
    inter = compute_grant_intersection(["a@example.com", "c@example.com"], conn)
    assert inter.get("data_package", frozenset()) == frozenset()


def test_intersection_unknown_email_denies_all(conn):
    from src.grant_intersection import compute_grant_intersection
    assert compute_grant_intersection(["a@example.com", "ghost@example.com"], conn) == {}


def test_intersection_empty_participant_list_denies_all(conn):
    from src.grant_intersection import compute_grant_intersection
    assert compute_grant_intersection([], conn) == {}
