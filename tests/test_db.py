"""Tests for src.db — DuckDB connection management and schema."""
import os
import tempfile

import duckdb
import pytest


def _setup_data_dir(tmp_path):
    os.environ["DATA_DIR"] = str(tmp_path)


class TestGetSystemDb:
    def test_creates_all_tables(self, tmp_path):
        _setup_data_dir(tmp_path)
        from src.db import get_system_db

        conn = get_system_db()
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
                ).fetchall()
            }
            expected = {
                "schema_version", "users", "sync_state", "sync_history",
                "user_sync_settings", "knowledge_items", "knowledge_votes",
                "audit_log", "telegram_links", "pending_codes",
                "script_registry", "table_registry", "table_profiles",
                "dataset_permissions",
            }
            assert expected.issubset(tables), f"Missing: {expected - tables}"
        finally:
            conn.close()

    def test_idempotent(self, tmp_path):
        _setup_data_dir(tmp_path)
        from src.db import get_system_db

        conn = get_system_db()
        conn.execute(
            "INSERT INTO users (id, email, name, role) VALUES ('u1', 'test@test.com', 'Test', 'analyst')"
        )
        conn.close()

        conn2 = get_system_db()
        try:
            result = conn2.execute("SELECT email FROM users WHERE id='u1'").fetchone()
            assert result[0] == "test@test.com"
        finally:
            conn2.close()


class TestGetSchemaVersion:
    def test_returns_version(self, tmp_path):
        _setup_data_dir(tmp_path)
        from src.db import get_schema_version, get_system_db

        conn = get_system_db()
        try:
            assert get_schema_version(conn) == 1
        finally:
            conn.close()

    def test_returns_zero_for_empty_db(self, tmp_path):
        _setup_data_dir(tmp_path)
        from src.db import get_schema_version

        conn = duckdb.connect(str(tmp_path / "empty.duckdb"))
        try:
            assert get_schema_version(conn) == 0
        finally:
            conn.close()


class TestGetAnalyticsDb:
    def test_creates_db(self, tmp_path):
        _setup_data_dir(tmp_path)
        from src.db import get_analytics_db

        conn = get_analytics_db()
        try:
            assert (tmp_path / "analytics" / "server.duckdb").exists()
        finally:
            conn.close()
