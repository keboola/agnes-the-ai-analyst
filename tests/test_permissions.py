"""Tests for sync settings, dataset permissions, and script execution."""

import os
import pytest


@pytest.fixture
def db_conn(tmp_path):
    os.environ["DATA_DIR"] = str(tmp_path)
    from src.db import get_system_db
    conn = get_system_db()
    yield conn
    conn.close()


class TestSyncSettingsRepository:
    def test_set_and_get(self, db_conn):
        from src.repositories.sync_settings import SyncSettingsRepository
        repo = SyncSettingsRepository(db_conn)
        repo.set_dataset_enabled("u1", "sales", True)
        repo.set_dataset_enabled("u1", "support", False)
        settings = repo.get_user_settings("u1")
        assert len(settings) == 2

    def test_is_enabled(self, db_conn):
        from src.repositories.sync_settings import SyncSettingsRepository
        repo = SyncSettingsRepository(db_conn)
        repo.set_dataset_enabled("u1", "sales", True)
        assert repo.is_dataset_enabled("u1", "sales") is True
        assert repo.is_dataset_enabled("u1", "support") is False

    def test_get_enabled_datasets(self, db_conn):
        from src.repositories.sync_settings import SyncSettingsRepository
        repo = SyncSettingsRepository(db_conn)
        repo.set_dataset_enabled("u1", "sales", True)
        repo.set_dataset_enabled("u1", "support", False)
        repo.set_dataset_enabled("u1", "hr", True)
        enabled = repo.get_enabled_datasets("u1")
        assert set(enabled) == {"sales", "hr"}

    def test_toggle_dataset(self, db_conn):
        from src.repositories.sync_settings import SyncSettingsRepository
        repo = SyncSettingsRepository(db_conn)
        repo.set_dataset_enabled("u1", "sales", True)
        assert repo.is_dataset_enabled("u1", "sales") is True
        repo.set_dataset_enabled("u1", "sales", False)
        assert repo.is_dataset_enabled("u1", "sales") is False


class TestDatasetPermissionRepository:
    def test_grant_and_check(self, db_conn):
        from src.repositories.sync_settings import DatasetPermissionRepository
        repo = DatasetPermissionRepository(db_conn)
        repo.grant("u1", "sales", "read")
        assert repo.has_access("u1", "sales") is True
        assert repo.has_access("u1", "hr") is False

    def test_revoke(self, db_conn):
        from src.repositories.sync_settings import DatasetPermissionRepository
        repo = DatasetPermissionRepository(db_conn)
        repo.grant("u1", "sales", "read")
        repo.revoke("u1", "sales")
        assert repo.has_access("u1", "sales") is False

    def test_get_accessible_datasets(self, db_conn):
        from src.repositories.sync_settings import DatasetPermissionRepository
        repo = DatasetPermissionRepository(db_conn)
        repo.grant("u1", "sales", "read")
        repo.grant("u1", "hr", "read")
        repo.grant("u1", "finance", "none")
        accessible = repo.get_accessible_datasets("u1")
        assert set(accessible) == {"sales", "hr"}

    def test_get_user_permissions(self, db_conn):
        from src.repositories.sync_settings import DatasetPermissionRepository
        repo = DatasetPermissionRepository(db_conn)
        repo.grant("u1", "sales", "read")
        repo.grant("u1", "hr", "read")
        perms = repo.get_user_permissions("u1")
        assert len(perms) == 2
