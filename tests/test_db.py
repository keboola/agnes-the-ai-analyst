"""Tests for src.db — DuckDB connection management and schema."""
import os
import tempfile

import duckdb
import pytest


def _setup_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))


class TestGetSystemDb:
    def test_creates_all_tables(self, tmp_path, monkeypatch):
        _setup_data_dir(tmp_path, monkeypatch)
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
                "dataset_permissions", "metric_definitions", "column_metadata",
            }
            assert expected.issubset(tables), f"Missing: {expected - tables}"
        finally:
            conn.close()

    def test_idempotent(self, tmp_path, monkeypatch):
        _setup_data_dir(tmp_path, monkeypatch)
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
    def test_returns_version(self, tmp_path, monkeypatch):
        _setup_data_dir(tmp_path, monkeypatch)
        from src.db import get_schema_version, get_system_db, SCHEMA_VERSION

        conn = get_system_db()
        try:
            assert get_schema_version(conn) == SCHEMA_VERSION
        finally:
            conn.close()

    def test_returns_zero_for_empty_db(self, tmp_path, monkeypatch):
        _setup_data_dir(tmp_path, monkeypatch)
        from src.db import get_schema_version

        conn = duckdb.connect(str(tmp_path / "empty.duckdb"))
        try:
            assert get_schema_version(conn) == 0
        finally:
            conn.close()


class TestV1ToV2Migration:
    def test_migration_adds_source_columns(self, tmp_path, monkeypatch):
        """Simulate a v1 database and verify v2 migration adds new columns."""
        _setup_data_dir(tmp_path, monkeypatch)
        import duckdb as _duckdb

        # Create a v1 database manually
        db_path = tmp_path / "state" / "system.duckdb"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = _duckdb.connect(str(db_path))
        conn.execute("""
            CREATE TABLE schema_version (version INTEGER, applied_at TIMESTAMP DEFAULT current_timestamp);
            INSERT INTO schema_version (version) VALUES (1);
            CREATE TABLE table_registry (
                id VARCHAR PRIMARY KEY, name VARCHAR NOT NULL, folder VARCHAR,
                sync_strategy VARCHAR, primary_key VARCHAR, description TEXT,
                registered_by VARCHAR, registered_at TIMESTAMP DEFAULT current_timestamp
            );
            INSERT INTO table_registry (id, name, folder) VALUES ('t1', 'Test', 'f1');
        """)
        # Create other required tables so _ensure_schema doesn't fail
        conn.execute("CREATE TABLE IF NOT EXISTS users (id VARCHAR PRIMARY KEY, email VARCHAR)")
        conn.execute("CREATE TABLE IF NOT EXISTS sync_state (table_id VARCHAR PRIMARY KEY)")
        conn.execute("CREATE TABLE IF NOT EXISTS sync_history (id VARCHAR PRIMARY KEY, table_id VARCHAR)")
        conn.execute("CREATE TABLE IF NOT EXISTS user_sync_settings (user_id VARCHAR, dataset VARCHAR, PRIMARY KEY(user_id, dataset))")
        conn.execute("CREATE TABLE IF NOT EXISTS knowledge_items (id VARCHAR PRIMARY KEY, title VARCHAR)")
        conn.execute("CREATE TABLE IF NOT EXISTS knowledge_votes (item_id VARCHAR, user_id VARCHAR, PRIMARY KEY(item_id, user_id))")
        conn.execute("CREATE TABLE IF NOT EXISTS audit_log (id VARCHAR PRIMARY KEY, action VARCHAR)")
        conn.execute("CREATE TABLE IF NOT EXISTS telegram_links (user_id VARCHAR PRIMARY KEY, chat_id BIGINT)")
        conn.execute("CREATE TABLE IF NOT EXISTS pending_codes (code VARCHAR PRIMARY KEY, chat_id BIGINT)")
        conn.execute("CREATE TABLE IF NOT EXISTS script_registry (id VARCHAR PRIMARY KEY, name VARCHAR, source TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS table_profiles (table_id VARCHAR PRIMARY KEY, profile JSON)")
        conn.execute("CREATE TABLE IF NOT EXISTS dataset_permissions (user_id VARCHAR, dataset VARCHAR, PRIMARY KEY(user_id, dataset))")
        conn.close()

        # Now open via get_system_db which should run migration
        from src.db import get_system_db, get_schema_version
        conn2 = get_system_db()
        try:
            from src.db import SCHEMA_VERSION
            assert get_schema_version(conn2) == SCHEMA_VERSION
            # Verify old data preserved
            row = conn2.execute("SELECT name, folder FROM table_registry WHERE id='t1'").fetchone()
            assert row[0] == "Test"
            assert row[1] == "f1"
            # Verify new columns exist
            cols = {r[0] for r in conn2.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name='table_registry'"
            ).fetchall()}
            assert "source_type" in cols
            assert "bucket" in cols
            assert "source_table" in cols
            assert "query_mode" in cols
            assert "sync_schedule" in cols
            assert "profile_after_sync" in cols
        finally:
            conn2.close()


class TestGetAnalyticsDb:
    def test_creates_db(self, tmp_path, monkeypatch):
        _setup_data_dir(tmp_path, monkeypatch)
        from src.db import get_analytics_db

        conn = get_analytics_db()
        try:
            assert (tmp_path / "analytics" / "server.duckdb").exists()
        finally:
            conn.close()


class TestMigrationSafety:
    """Tests for schema migration correctness, idempotency, and safety snapshots."""

    # Minimal v2 table_registry (no is_public column — that comes in v3)
    _V2_TABLE_REGISTRY = """
        CREATE TABLE table_registry (
            id VARCHAR PRIMARY KEY,
            name VARCHAR NOT NULL,
            source_type VARCHAR,
            bucket VARCHAR,
            source_table VARCHAR,
            sync_strategy VARCHAR DEFAULT 'full_refresh',
            query_mode VARCHAR DEFAULT 'local',
            sync_schedule VARCHAR,
            profile_after_sync BOOLEAN DEFAULT true,
            primary_key VARCHAR,
            folder VARCHAR,
            description TEXT,
            registered_by VARCHAR,
            registered_at TIMESTAMP DEFAULT current_timestamp
        );
    """

    def _create_v2_db(self, db_path):
        """Create a minimal v2-schema DuckDB file at db_path."""
        import duckdb as _duckdb
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = _duckdb.connect(str(db_path))
        try:
            conn.execute(
                "CREATE TABLE schema_version (version INTEGER, applied_at TIMESTAMP DEFAULT current_timestamp);"
                "INSERT INTO schema_version (version) VALUES (2);"
            )
            conn.execute(self._V2_TABLE_REGISTRY)
            # Stub out remaining tables so _ensure_schema doesn't fail
            for ddl in [
                "CREATE TABLE IF NOT EXISTS users (id VARCHAR PRIMARY KEY, email VARCHAR)",
                "CREATE TABLE IF NOT EXISTS sync_state (table_id VARCHAR PRIMARY KEY)",
                "CREATE TABLE IF NOT EXISTS sync_history (id VARCHAR PRIMARY KEY, table_id VARCHAR)",
                "CREATE TABLE IF NOT EXISTS user_sync_settings (user_id VARCHAR, dataset VARCHAR, PRIMARY KEY(user_id, dataset))",
                "CREATE TABLE IF NOT EXISTS knowledge_items (id VARCHAR PRIMARY KEY, title VARCHAR)",
                "CREATE TABLE IF NOT EXISTS knowledge_votes (item_id VARCHAR, user_id VARCHAR, PRIMARY KEY(item_id, user_id))",
                "CREATE TABLE IF NOT EXISTS audit_log (id VARCHAR PRIMARY KEY, action VARCHAR)",
                "CREATE TABLE IF NOT EXISTS telegram_links (user_id VARCHAR PRIMARY KEY, chat_id BIGINT)",
                "CREATE TABLE IF NOT EXISTS pending_codes (code VARCHAR PRIMARY KEY, chat_id BIGINT)",
                "CREATE TABLE IF NOT EXISTS script_registry (id VARCHAR PRIMARY KEY, name VARCHAR, source TEXT)",
                "CREATE TABLE IF NOT EXISTS table_profiles (table_id VARCHAR PRIMARY KEY, profile JSON)",
                "CREATE TABLE IF NOT EXISTS dataset_permissions (user_id VARCHAR, dataset VARCHAR, PRIMARY KEY(user_id, dataset))",
            ]:
                conn.execute(ddl)
        finally:
            conn.close()

    def test_v2_to_v3_migration(self, tmp_path, monkeypatch):
        """v2 DB migrated to current schema: is_public column added."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        import duckdb as _duckdb
        from src.db import _ensure_schema, get_schema_version, SCHEMA_VERSION

        db_path = tmp_path / "state" / "system.duckdb"
        self._create_v2_db(db_path)

        conn = _duckdb.connect(str(db_path))
        try:
            _ensure_schema(conn)
            assert get_schema_version(conn) == SCHEMA_VERSION
            cols = {
                r[0]
                for r in conn.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_name='table_registry'"
                ).fetchall()
            }
            assert "is_public" in cols
        finally:
            conn.close()

    def test_migration_idempotency(self, tmp_path, monkeypatch):
        """Calling _ensure_schema twice on a fresh DB raises no error and leaves version at 3."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        import duckdb as _duckdb
        from src.db import _ensure_schema, get_schema_version, SCHEMA_VERSION

        db_path = tmp_path / "state" / "system.duckdb"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = _duckdb.connect(str(db_path))
        try:
            _ensure_schema(conn)
            _ensure_schema(conn)
            assert get_schema_version(conn) == SCHEMA_VERSION
        finally:
            conn.close()

    def test_migration_preserves_data(self, tmp_path, monkeypatch):
        """Data inserted before migration is preserved after migration runs."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        import duckdb as _duckdb
        from src.db import _ensure_schema, get_schema_version, _SYSTEM_SCHEMA

        db_path = tmp_path / "state" / "system.duckdb"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = _duckdb.connect(str(db_path))
        try:
            # Build a v1 schema manually
            conn.execute(
                "CREATE TABLE schema_version (version INTEGER, applied_at TIMESTAMP DEFAULT current_timestamp);"
                "INSERT INTO schema_version (version) VALUES (1);"
            )
            conn.execute("""
                CREATE TABLE table_registry (
                    id VARCHAR PRIMARY KEY,
                    name VARCHAR NOT NULL,
                    folder VARCHAR,
                    sync_strategy VARCHAR,
                    primary_key VARCHAR,
                    description TEXT,
                    registered_by VARCHAR,
                    registered_at TIMESTAMP DEFAULT current_timestamp
                );
            """)
            conn.execute(
                "INSERT INTO table_registry (id, name, description) VALUES ('row1', 'MyTable', 'kept')"
            )
            # Stub remaining tables
            for ddl in [
                "CREATE TABLE IF NOT EXISTS users (id VARCHAR PRIMARY KEY, email VARCHAR)",
                "CREATE TABLE IF NOT EXISTS sync_state (table_id VARCHAR PRIMARY KEY)",
                "CREATE TABLE IF NOT EXISTS sync_history (id VARCHAR PRIMARY KEY, table_id VARCHAR)",
                "CREATE TABLE IF NOT EXISTS user_sync_settings (user_id VARCHAR, dataset VARCHAR, PRIMARY KEY(user_id, dataset))",
                "CREATE TABLE IF NOT EXISTS knowledge_items (id VARCHAR PRIMARY KEY, title VARCHAR)",
                "CREATE TABLE IF NOT EXISTS knowledge_votes (item_id VARCHAR, user_id VARCHAR, PRIMARY KEY(item_id, user_id))",
                "CREATE TABLE IF NOT EXISTS audit_log (id VARCHAR PRIMARY KEY, action VARCHAR)",
                "CREATE TABLE IF NOT EXISTS telegram_links (user_id VARCHAR PRIMARY KEY, chat_id BIGINT)",
                "CREATE TABLE IF NOT EXISTS pending_codes (code VARCHAR PRIMARY KEY, chat_id BIGINT)",
                "CREATE TABLE IF NOT EXISTS script_registry (id VARCHAR PRIMARY KEY, name VARCHAR, source TEXT)",
                "CREATE TABLE IF NOT EXISTS table_profiles (table_id VARCHAR PRIMARY KEY, profile JSON)",
                "CREATE TABLE IF NOT EXISTS dataset_permissions (user_id VARCHAR, dataset VARCHAR, PRIMARY KEY(user_id, dataset))",
            ]:
                conn.execute(ddl)

            _ensure_schema(conn)

            from src.db import SCHEMA_VERSION
            assert get_schema_version(conn) == SCHEMA_VERSION
            row = conn.execute(
                "SELECT name, description FROM table_registry WHERE id='row1'"
            ).fetchone()
            assert row is not None, "Pre-migration row was lost"
            assert row[0] == "MyTable"
            assert row[1] == "kept"
        finally:
            conn.close()

    def test_pre_migration_snapshot_created(self, tmp_path, monkeypatch):
        """A pre-migrate snapshot is written when migrating an existing (non-fresh) DB."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        from src.db import get_system_db

        # Create a v2 DB at the expected path before calling get_system_db
        db_path = tmp_path / "state" / "system.duckdb"
        self._create_v2_db(db_path)

        conn = get_system_db()
        try:
            snapshot = tmp_path / "state" / "system.duckdb.pre-migrate"
            assert snapshot.exists(), "Pre-migration snapshot was not created"
        finally:
            conn.close()

    def test_no_snapshot_on_fresh_db(self, tmp_path, monkeypatch):
        """No pre-migrate snapshot is created when initialising a brand-new DB."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        from src.db import get_system_db

        conn = get_system_db()
        try:
            snapshot = tmp_path / "state" / "system.duckdb.pre-migrate"
            assert not snapshot.exists(), "Snapshot should not exist for a fresh DB"
        finally:
            conn.close()

    def test_future_version_is_noop(self, tmp_path, monkeypatch):
        """_ensure_schema does nothing when schema_version > SCHEMA_VERSION."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        import duckdb as _duckdb
        from src.db import _ensure_schema, get_schema_version

        db_path = tmp_path / "state" / "system.duckdb"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = _duckdb.connect(str(db_path))
        try:
            conn.execute(
                "CREATE TABLE schema_version (version INTEGER, applied_at TIMESTAMP DEFAULT current_timestamp);"
                "INSERT INTO schema_version (version) VALUES (99);"
            )
            _ensure_schema(conn)
            assert get_schema_version(conn) == 99
        finally:
            conn.close()


class TestSchemaV4:
    """Tests for v4 schema additions: metric_definitions and column_metadata tables."""

    def test_metric_definitions_table_exists(self, tmp_path, monkeypatch):
        """metric_definitions and column_metadata tables exist after get_system_db()."""
        _setup_data_dir(tmp_path, monkeypatch)
        from src.db import get_system_db

        conn = get_system_db()
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
                ).fetchall()
            }
            assert "metric_definitions" in tables, "metric_definitions table missing"
            assert "column_metadata" in tables, "column_metadata table missing"
        finally:
            conn.close()

    def test_metric_definitions_columns(self, tmp_path, monkeypatch):
        """metric_definitions table has all expected columns."""
        _setup_data_dir(tmp_path, monkeypatch)
        from src.db import get_system_db

        conn = get_system_db()
        try:
            cols = {
                row[0]
                for row in conn.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_name = 'metric_definitions'"
                ).fetchall()
            }
            expected = {
                "id", "name", "display_name", "category", "description",
                "type", "unit", "grain", "table_name", "tables",
                "expression", "time_column", "dimensions", "filters",
                "synonyms", "notes", "sql", "sql_variants", "validation",
                "source", "created_at", "updated_at",
            }
            assert expected.issubset(cols), f"Missing columns: {expected - cols}"
        finally:
            conn.close()

    def test_column_metadata_table_exists(self, tmp_path, monkeypatch):
        """column_metadata table has all expected columns."""
        _setup_data_dir(tmp_path, monkeypatch)
        from src.db import get_system_db

        conn = get_system_db()
        try:
            cols = {
                row[0]
                for row in conn.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_name = 'column_metadata'"
                ).fetchall()
            }
            expected = {
                "table_id", "column_name", "basetype", "description",
                "confidence", "source", "updated_at",
            }
            assert expected.issubset(cols), f"Missing columns: {expected - cols}"
        finally:
            conn.close()

    def test_v3_to_v4_migration(self, tmp_path, monkeypatch):
        """Simulate a v3 database, call get_system_db(), verify it migrates to v4."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        import duckdb as _duckdb
        from src.db import get_system_db, get_schema_version, SCHEMA_VERSION

        # Build a minimal v3 database manually
        db_path = tmp_path / "state" / "system.duckdb"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = _duckdb.connect(str(db_path))
        try:
            conn.execute(
                "CREATE TABLE schema_version (version INTEGER, applied_at TIMESTAMP DEFAULT current_timestamp);"
                "INSERT INTO schema_version (version) VALUES (3);"
            )
            # Create the tables that exist in v3 (minimal stubs)
            conn.execute("CREATE TABLE IF NOT EXISTS table_registry (id VARCHAR PRIMARY KEY, name VARCHAR NOT NULL, is_public BOOLEAN DEFAULT true)")
            for ddl in [
                "CREATE TABLE IF NOT EXISTS users (id VARCHAR PRIMARY KEY, email VARCHAR)",
                "CREATE TABLE IF NOT EXISTS sync_state (table_id VARCHAR PRIMARY KEY)",
                "CREATE TABLE IF NOT EXISTS sync_history (id VARCHAR PRIMARY KEY, table_id VARCHAR)",
                "CREATE TABLE IF NOT EXISTS user_sync_settings (user_id VARCHAR, dataset VARCHAR, PRIMARY KEY(user_id, dataset))",
                "CREATE TABLE IF NOT EXISTS knowledge_items (id VARCHAR PRIMARY KEY, title VARCHAR)",
                "CREATE TABLE IF NOT EXISTS knowledge_votes (item_id VARCHAR, user_id VARCHAR, PRIMARY KEY(item_id, user_id))",
                "CREATE TABLE IF NOT EXISTS audit_log (id VARCHAR PRIMARY KEY, action VARCHAR)",
                "CREATE TABLE IF NOT EXISTS telegram_links (user_id VARCHAR PRIMARY KEY, chat_id BIGINT)",
                "CREATE TABLE IF NOT EXISTS pending_codes (code VARCHAR PRIMARY KEY, chat_id BIGINT)",
                "CREATE TABLE IF NOT EXISTS script_registry (id VARCHAR PRIMARY KEY, name VARCHAR, source TEXT)",
                "CREATE TABLE IF NOT EXISTS table_profiles (table_id VARCHAR PRIMARY KEY, profile JSON)",
                "CREATE TABLE IF NOT EXISTS dataset_permissions (user_id VARCHAR, dataset VARCHAR, PRIMARY KEY(user_id, dataset))",
                "CREATE TABLE IF NOT EXISTS access_requests (id VARCHAR PRIMARY KEY, user_id VARCHAR, user_email VARCHAR, table_id VARCHAR)",
            ]:
                conn.execute(ddl)
        finally:
            conn.close()

        conn2 = get_system_db()
        try:
            assert get_schema_version(conn2) == SCHEMA_VERSION, f"Expected version {SCHEMA_VERSION}, got {get_schema_version(conn2)}"
            tables = {
                row[0]
                for row in conn2.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
                ).fetchall()
            }
            assert "metric_definitions" in tables, "metric_definitions table missing after migration"
            assert "column_metadata" in tables, "column_metadata table missing after migration"
        finally:
            conn2.close()


class TestGetAnalyticsDbReadonly:
    def test_analytics_readonly_rejects_malicious_dir_name(self, tmp_path, monkeypatch):
        """Directories with SQL-injection chars in their name are skipped."""
        _setup_data_dir(tmp_path, monkeypatch)
        import importlib
        import src.db as db_module
        importlib.reload(db_module)

        # Create the analytics DB first so get_analytics_db_readonly takes the read_only path
        analytics_dir = tmp_path / "analytics"
        analytics_dir.mkdir(parents=True, exist_ok=True)
        import duckdb as _duckdb
        seed_conn = _duckdb.connect(str(analytics_dir / "server.duckdb"))
        seed_conn.close()

        # Create a malicious extract directory whose name contains SQL injection chars
        malicious_name = "foo) AS x; DROP TABLE users; --"
        ext_dir = tmp_path / "extracts" / malicious_name
        ext_dir.mkdir(parents=True, exist_ok=True)
        # Place a real (empty) extract.duckdb inside it
        mal_conn = _duckdb.connect(str(ext_dir / "extract.duckdb"))
        mal_conn.close()

        # get_analytics_db_readonly must not raise and must skip the malicious dir
        conn = db_module.get_analytics_db_readonly()
        try:
            # Verify no attachment was made for the malicious source name
            attached = {
                row[0]
                for row in conn.execute(
                    "SELECT database_name FROM duckdb_databases()"
                ).fetchall()
            }
            assert malicious_name not in attached
        finally:
            conn.close()
