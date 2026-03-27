"""Tests for src.db — DuckDB connection management and schema."""
import os
import tempfile

import duckdb
import pytest


def _setup_data_dir(tmp_path):
    """Set DATA_DIR env var to a temporary directory."""
    os.environ["DATA_DIR"] = str(tmp_path)


class TestGetSystemDb:
    """Tests for get_system_db()."""

    def test_get_system_db_creates_tables(self, tmp_path):
        _setup_data_dir(tmp_path)
        from src.db import get_system_db

        conn = get_system_db()
        try:
            tables = [
                row[0]
                for row in conn.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'main' ORDER BY table_name"
                ).fetchall()
            ]
            expected = sorted([
                "schema_version",
                "users",
                "sync_state",
                "sync_history",
                "user_sync_settings",
                "knowledge_items",
                "knowledge_votes",
                "audit_log",
                "telegram_links",
                "pending_codes",
                "script_registry",
                "table_registry",
                "table_profiles",
                "dataset_permissions",
            ])
            assert tables == expected
        finally:
            conn.close()

    def test_get_system_db_idempotent(self, tmp_path):
        _setup_data_dir(tmp_path)
        from src.db import get_system_db

        conn = get_system_db()
        conn.execute(
            "INSERT INTO users (email, name) VALUES ('test@example.com', 'Test')"
        )
        conn.close()

        conn2 = get_system_db()
        try:
            rows = conn2.execute("SELECT email FROM users").fetchall()
            assert len(rows) == 1
            assert rows[0][0] == "test@example.com"
        finally:
            conn2.close()


class TestGetSchemaVersion:
    """Tests for get_schema_version()."""

    def test_get_schema_version(self, tmp_path):
        _setup_data_dir(tmp_path)
        from src.db import get_schema_version, get_system_db

        conn = get_system_db()
        try:
            assert get_schema_version(conn) == 1
        finally:
            conn.close()

    def test_get_schema_version_no_table(self, tmp_path):
        _setup_data_dir(tmp_path)
        from src.db import get_schema_version

        db_path = tmp_path / "empty.duckdb"
        conn = duckdb.connect(str(db_path))
        try:
            assert get_schema_version(conn) == 0
        finally:
            conn.close()


class TestGetAnalyticsDb:
    """Tests for get_analytics_db()."""

    def test_get_analytics_db(self, tmp_path):
        _setup_data_dir(tmp_path)
        from src.db import get_analytics_db

        conn = get_analytics_db()
        try:
            assert (tmp_path / "analytics" / "server.duckdb").exists()
        finally:
            conn.close()
