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


def test_manifest_includes_query_mode_for_local_table(tmp_path, monkeypatch):
    """Local-mode table must surface query_mode='local' in manifest."""
    db_module = _reload_db_module(monkeypatch, tmp_path)

    from src.repositories.sync_state import SyncStateRepository
    from src.repositories.table_registry import TableRegistryRepository
    from app.api.sync import _build_manifest_for_user

    conn = db_module.get_system_db()
    try:
        TableRegistryRepository(conn).register(
            id="orders", name="orders", source_type="keboola",
            bucket="sales", source_table="orders", query_mode="local",
        )
        SyncStateRepository(conn).update_sync(
            table_id="orders", rows=10, file_size_bytes=1024, hash="abc",
        )
        admin = {"role": "admin", "email": "a@x.com"}
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
        TableRegistryRepository(conn).register(
            id="bq_view", name="bq_view", source_type="bigquery",
            bucket="ds", source_table="bq_view", query_mode="remote",
        )
        SyncStateRepository(conn).update_sync(
            table_id="bq_view", rows=0, file_size_bytes=0, hash="",
        )
        admin = {"role": "admin", "email": "a@x.com"}
        manifest = _build_manifest_for_user(conn, admin)
        assert manifest["tables"]["bq_view"]["query_mode"] == "remote"
        assert manifest["tables"]["bq_view"]["source_type"] == "bigquery"
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
        SyncStateRepository(conn).update_sync(
            table_id="orphan", rows=0, file_size_bytes=0, hash="",
        )
        admin = {"role": "admin", "email": "a@x.com"}
        manifest = _build_manifest_for_user(conn, admin)
        assert manifest["tables"]["orphan"]["query_mode"] == "local"
        assert manifest["tables"]["orphan"]["source_type"] == ""
    finally:
        conn.close()
