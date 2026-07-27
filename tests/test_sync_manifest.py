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


# ---------------------------------------------------------------------------
# parts[] surfaced in the manifest (partitioned distribution). A partitioned
# table's per-part list rides through to the manifest entry; single-file
# tables carry parts=None (backward compatible).
# ---------------------------------------------------------------------------


def test_manifest_entry_includes_parts_for_partitioned_table():
    from app.api.sync import _table_manifest_entry

    parts = [
        {"path": "month=2026-06/data.parquet", "hash": "aa", "size_bytes": 100},
        {"path": "month=2026-07/data.parquet", "hash": "bb", "size_bytes": 250},
    ]
    state = {
        "table_id": "issues", "hash": "rollup", "file_size_bytes": 350,
        "rows": 5, "parts": parts, "last_sync": None,
    }
    reg = {"id": "issues", "name": "issues", "query_mode": "local", "source_type": "jira"}

    entry = _table_manifest_entry(state, reg)
    assert entry["parts"] == parts
    # Whole-table fields still present for the single-compare fast path.
    assert entry["hash"] == "rollup"
    assert entry["size_bytes"] == 350


def test_manifest_entry_parts_none_for_single_file_table():
    from app.api.sync import _table_manifest_entry

    state = {"table_id": "account", "hash": "h", "file_size_bytes": 10, "rows": 1, "last_sync": None}
    reg = {"id": "account", "name": "account", "query_mode": "materialized", "source_type": "keboola"}

    entry = _table_manifest_entry(state, reg)
    assert entry["parts"] is None


def test_flat_manifest_tables_dict_carries_parts_for_partitioned(tmp_path, monkeypatch):
    """REGRESSION (Devin #1): the FLAT `manifest['tables']` dict — the one
    `cli/lib/pull.py:run_pull` actually reads for download decisions — must
    carry `parts` for a partitioned table, else the client never routes it to
    the per-part sync and tries a single-file download that 404s."""
    db_module = _reload_db_module(monkeypatch, tmp_path)

    from src.repositories.sync_state import SyncStateRepository
    from src.repositories.table_registry import TableRegistryRepository
    from app.api.sync import _build_manifest_for_user

    conn = db_module.get_system_db()
    try:
        _ensure_admin1(conn)
        TableRegistryRepository(conn).register(
            id="issues", name="issues", source_type="jira",
            bucket="", source_table="issues", query_mode="local",
        )
        TableRegistryRepository(conn).register(
            id="account", name="account", source_type="keboola",
            bucket="sales", source_table="account", query_mode="local",
        )
        parts = [
            {"path": "month=2026-06/data.parquet", "hash": "aa", "size_bytes": 100},
            {"path": "month=2026-07/data.parquet", "hash": "bb", "size_bytes": 250},
        ]
        SyncStateRepository(conn).update_sync(
            table_id="issues", rows=5, file_size_bytes=350, hash="rollup", parts=parts)
        SyncStateRepository(conn).update_sync(
            table_id="account", rows=9, file_size_bytes=90, hash="h")  # single-file

        manifest = _build_manifest_for_user(conn, {"id": "admin1", "email": "a@x.com"})
        assert manifest["tables"]["issues"]["parts"] == parts
        assert manifest["tables"]["account"]["parts"] is None
    finally:
        conn.close()
