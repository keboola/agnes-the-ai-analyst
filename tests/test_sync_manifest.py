"""Tests for /api/sync/manifest — query_mode and source_type per table.

These tests target the `_build_manifest_for_user` helper directly so they can
exercise the query_mode/source_type joining logic without going through the
HTTP layer. The CLI (Task 7) relies on these fields to skip remote-mode
tables during download.
"""

import importlib


def _reload_db_module(monkeypatch, tmp_path):
    """Point DATA_DIR at tmp_path and reload db module so paths take effect."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "state").mkdir(exist_ok=True)
    import src.db as db_module

    importlib.reload(db_module)
    return db_module


def _ensure_admin1(conn):
    """Seed an admin user with id='admin1' + Admin group membership so
    {"id": "admin1", ...} dicts pass the can_access admin shortcut."""
    from src.db import SYSTEM_ADMIN_GROUP
    from src.repositories.users import UserRepository
    from src.repositories.user_group_members import UserGroupMembersRepository

    if UserRepository(conn).get_by_id("admin1") is None:
        UserRepository(conn).create(id="admin1", email="admin1@test.com", name="Admin")
    admin_gid = conn.execute("SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]).fetchone()
    if admin_gid:
        UserGroupMembersRepository(conn).add_member(
            "admin1",
            admin_gid[0],
            source="system_seed",
        )


def test_manifest_includes_query_mode_for_local_table(tmp_path, monkeypatch):
    """Local-mode table must surface query_mode='local' in manifest."""
    db_module = _reload_db_module(monkeypatch, tmp_path)

    from src.repositories.sync_state import SyncStateRepository
    from src.repositories.table_registry import TableRegistryRepository
    from app.api.sync import _build_manifest_for_user

    conn = db_module.get_system_db()
    try:
        _ensure_admin1(conn)
        TableRegistryRepository(conn).register(
            id="orders",
            name="orders",
            source_type="keboola",
            bucket="sales",
            source_table="orders",
            query_mode="local",
        )
        SyncStateRepository(conn).update_sync(
            table_id="orders",
            rows=10,
            file_size_bytes=1024,
            hash="abc",
        )
        admin = {"id": "admin1", "email": "a@x.com"}
        manifest = _build_manifest_for_user(conn, admin)
        assert manifest["tables"]["orders"]["query_mode"] == "local"
        assert manifest["tables"]["orders"]["source_type"] == "keboola"
        assert manifest["tables"]["orders"]["hash"] == "abc"
        assert manifest["tables"]["orders"]["rows"] == 10
        assert manifest["tables"]["orders"]["size_bytes"] == 1024
    finally:
        conn.close()


def test_manifest_includes_query_mode_for_remote_table(tmp_path, monkeypatch):
    """Remote-mode table (BQ) must surface query_mode='remote' in manifest."""
    db_module = _reload_db_module(monkeypatch, tmp_path)

    from src.repositories.sync_state import SyncStateRepository
    from src.repositories.table_registry import TableRegistryRepository
    from app.api.sync import _build_manifest_for_user

    conn = db_module.get_system_db()
    try:
        _ensure_admin1(conn)
        TableRegistryRepository(conn).register(
            id="bq_view",
            name="bq_view",
            source_type="bigquery",
            bucket="ds",
            source_table="bq_view",
            query_mode="remote",
        )
        SyncStateRepository(conn).update_sync(
            table_id="bq_view",
            rows=0,
            file_size_bytes=0,
            hash="",
        )
        admin = {"id": "admin1", "email": "a@x.com"}
        manifest = _build_manifest_for_user(conn, admin)
        assert manifest["tables"]["bq_view"]["query_mode"] == "remote"
        assert manifest["tables"]["bq_view"]["source_type"] == "bigquery"
    finally:
        conn.close()


def test_manifest_filters_by_accessible_tables_for_analyst(tmp_path, monkeypatch):
    """Non-admin manifest filtering (FAI-132 N+1 collapse): the resolved
    accessible-id set must produce IDENTICAL membership to the old per-row
    ``can_access_table`` filter — analyst sees only the packaged/granted
    table, not the ungranted one; admin still sees both."""
    db_module = _reload_db_module(monkeypatch, tmp_path)

    from src.repositories.sync_state import SyncStateRepository
    from src.repositories.table_registry import TableRegistryRepository
    from src.repositories.users import UserRepository
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.data_packages import DataPackagesRepository
    from app.api.sync import _build_manifest_for_user

    conn = db_module.get_system_db()
    try:
        _ensure_admin1(conn)
        UserRepository(conn).create(id="analyst1", email="analyst@test.com", name="Analyst")

        TableRegistryRepository(conn).register(
            id="orders",
            name="orders",
            source_type="keboola",
            bucket="sales",
            source_table="orders",
            query_mode="local",
        )
        TableRegistryRepository(conn).register(
            id="hidden",
            name="hidden",
            source_type="keboola",
            bucket="sales",
            source_table="hidden",
            query_mode="local",
        )
        SyncStateRepository(conn).update_sync(
            table_id="orders",
            rows=10,
            file_size_bytes=1024,
            hash="abc",
        )
        SyncStateRepository(conn).update_sync(
            table_id="hidden",
            rows=5,
            file_size_bytes=512,
            hash="def",
        )

        group = UserGroupsRepository(conn).create(name="ManifestGroup", description="", created_by="test")
        gid = group["id"] if isinstance(group, dict) else group
        UserGroupMembersRepository(conn).add_member("analyst1", gid, source="test")

        pkg_repo = DataPackagesRepository(conn)
        pkg_id = pkg_repo.create(
            name="OrdersPkg",
            slug="orders-pkg",
            description=None,
            icon=None,
            color=None,
            created_by="test",
        )
        pkg_repo.add_table(pkg_id, "orders", added_by="test")
        conn.execute(
            "INSERT INTO resource_grants(id, group_id, resource_type, resource_id, "
            "requirement, assigned_at, assigned_by) "
            "VALUES (?, ?, 'data_package', ?, 'required', CURRENT_TIMESTAMP, 'test')",
            ["grant-orders-pkg", gid, pkg_id],
        )

        analyst = {"id": "analyst1", "email": "analyst@test.com"}
        analyst_manifest = _build_manifest_for_user(conn, analyst)
        assert set(analyst_manifest["tables"].keys()) == {"orders"}

        admin = {"id": "admin1", "email": "a@x.com"}
        admin_manifest = _build_manifest_for_user(conn, admin)
        assert set(admin_manifest["tables"].keys()) == {"orders", "hidden"}
    finally:
        conn.close()


def test_manifest_package_table_name_is_path_safe(tmp_path, monkeypatch):
    """A packaged table with no sync_state falls back to the registry
    display name — when that contains path-unsafe characters (spaces, like
    the internal tables' \"Agnes audit log\"), the manifest must send the
    path-safe registry id instead, or the CLI's stack_sync stage aborts
    with \"unsafe path segment\"."""
    db_module = _reload_db_module(monkeypatch, tmp_path)

    from src.repositories.table_registry import TableRegistryRepository
    from src.repositories.data_packages import DataPackagesRepository
    from app.api.sync import _build_manifest_for_user

    conn = db_module.get_system_db()
    try:
        _ensure_admin1(conn)
        TableRegistryRepository(conn).register(
            id="agnes_audit",
            name="Agnes audit log",
            source_type="internal",
            bucket="internal",
            source_table="agnes_audit",
            query_mode="internal",
        )
        pkg_repo = DataPackagesRepository(conn)
        pkg_id = pkg_repo.create(
            name="Agnes Internal",
            slug="agnes-internal",
            description=None,
            icon=None,
            color=None,
            created_by="test",
        )
        pkg_repo.add_table(pkg_id, "agnes_audit", added_by="test")
        conn.execute(
            "INSERT INTO resource_grants(id, group_id, resource_type, resource_id, "
            "requirement, assigned_at, assigned_by) "
            "SELECT 'grant-internal-pkg', id, 'data_package', ?, 'required', "
            "CURRENT_TIMESTAMP, 'test' FROM user_groups WHERE name = 'Admin'",
            [pkg_id],
        )
        admin = {"id": "admin1", "email": "a@x.com"}
        manifest = _build_manifest_for_user(conn, admin)
        pkgs = {p["slug"]: p for p in manifest.get("data_packages") or []}
        assert "agnes-internal" in pkgs
        for t in pkgs["agnes-internal"]["tables"]:
            # Path-safe: no spaces or separators — the CLI's _SAFE_SEGMENT_RE.
            assert " " not in t["name"], f"unsafe manifest table name: {t['name']!r}"
            assert t["name"] == "agnes_audit"
            assert t["query_mode"] == "internal"
    finally:
        conn.close()


def test_manifest_defaults_query_mode_local_for_unregistered_state(tmp_path, monkeypatch):
    """Sync state without a corresponding registry row must default query_mode='local'.

    Defensive: if registry lookup misses (deleted entry, race), don't break the manifest.
    """
    db_module = _reload_db_module(monkeypatch, tmp_path)

    from src.repositories.sync_state import SyncStateRepository
    from app.api.sync import _build_manifest_for_user

    conn = db_module.get_system_db()
    try:
        _ensure_admin1(conn)
        SyncStateRepository(conn).update_sync(
            table_id="orphan",
            rows=0,
            file_size_bytes=0,
            hash="",
        )
        admin = {"id": "admin1", "email": "a@x.com"}
        manifest = _build_manifest_for_user(conn, admin)
        assert manifest["tables"]["orphan"]["query_mode"] == "local"
        assert manifest["tables"]["orphan"]["source_type"] == ""
    finally:
        conn.close()
