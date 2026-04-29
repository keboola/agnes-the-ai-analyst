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
        """``_ensure_schema`` does not modify ``schema_version`` when it's
        already past ``SCHEMA_VERSION``. The unconditional ``_SYSTEM_SCHEMA``
        self-heal pass *does* run on the future-version DB — it's all
        ``CREATE TABLE IF NOT EXISTS``, so tables this binary expects get
        materialized — but the version row stays put."""
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

    def test_split_brain_future_version_with_missing_tables_self_heals(
        self, tmp_path, monkeypatch,
    ):
        """Regression for a shared dev-VM split-brain incident.

        Shape: a contributor experiments with a future-schema branch that
        bumps the DB to ``schema_version=N`` (N > current binary's
        ``SCHEMA_VERSION``) with its own table layout, then switches or
        rebases back to the released binary. The on-disk DB is on a
        version this binary doesn't understand and is missing tables this
        binary's code expects. Without self-heal, every query against the
        missing table crashes at runtime — the migration block correctly
        skips (we don't downgrade), but nothing creates the missing
        tables either.

        The contract this test pins: the gated
        ``conn.execute(_SYSTEM_SCHEMA)`` call (run when ``current >=
        SCHEMA_VERSION``) materializes any missing tables *and* leaves
        the future-version ``schema_version`` row untouched. We
        synthesize a v99 DB whose only table is ``schema_version``,
        then assert that running ``_ensure_schema`` creates the v13-era
        core tables that the binary needs (``user_groups``,
        ``user_group_members``, ``resource_grants``, ``users``) while
        keeping the version at 99.
        """
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        import duckdb as _duckdb
        from src.db import _ensure_schema, get_schema_version

        db_path = tmp_path / "state" / "system.duckdb"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = _duckdb.connect(str(db_path))
        try:
            # Synthesize an "old binary on a future-schema DB" state: only
            # the schema_version table exists (no current-schema tables,
            # no lab tables either — matches the exact shape seen after
            # a lab migration ran but the binary then rolled back to one
            # that doesn't know the lab schema).
            conn.execute(
                "CREATE TABLE schema_version (version INTEGER, applied_at TIMESTAMP DEFAULT current_timestamp);"
                "INSERT INTO schema_version (version) VALUES (99);"
            )

            # Sanity: the v13-era tables we expect the self-heal pass to
            # create are NOT there before the call. Picked from the
            # post-RBAC-v13 / post-marketplace surface so a future
            # rename/drop in src/db.py fails this test loudly.
            expected_tables = {
                "users",
                "user_groups",
                "user_group_members",
                "resource_grants",
            }
            tables_before = {
                r[0]
                for r in conn.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = ?",
                    ["main"],
                ).fetchall()
            }
            assert not (expected_tables & tables_before), (
                "fixture started with a non-empty schema; expected only "
                "schema_version to be present"
            )

            _ensure_schema(conn)

            # After: every expected table exists (self-heal worked) AND
            # the version row stays at the future value.
            tables_after = {
                r[0]
                for r in conn.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = ?",
                    ["main"],
                ).fetchall()
            }
            missing = expected_tables - tables_after
            assert not missing, (
                f"self-heal must create v13-era tables on a future-version DB, "
                f"missing: {sorted(missing)}"
            )

            # The future-version contract still holds: version row untouched.
            assert get_schema_version(conn) == 99
        finally:
            conn.close()

    def test_pre_migration_snapshot_excludes_post_self_heal_tables(
        self, tmp_path, monkeypatch,
    ):
        """The pre-migration snapshot must capture the on-disk DB state
        *before* any DDL runs, so operators reading the snapshot for
        rollback debugging see the old schema as it actually was — not
        the binary's full table set with extras tacked on.

        Regression for the original hoist in 0.12.0: ``_SYSTEM_SCHEMA``
        was unconditionally executed at the top of ``_ensure_schema``,
        ahead of the snapshot copy in the migration block. On a v2→vN
        migration, ``view_ownership`` / ``user_groups`` /
        ``resource_grants`` (and every other table the modern binary
        adds) were created first, then ``CHECKPOINT`` flushed them to
        disk, and ``shutil.copy2`` copied the already-modified file as
        the "pre-migration" snapshot. Functionally rollback still
        worked (extra empty tables are harmless), but the snapshot was
        misleading. Fix: gate the self-heal call on ``current >=
        SCHEMA_VERSION`` so the migration path takes its snapshot
        before any DDL touches the DB.
        """
        from src.db import (
            SCHEMA_VERSION,
            _ensure_schema,
            get_schema_version,
            get_system_db,
        )

        # Bootstrap a v2 DB on disk, then trigger the migration ladder.
        db_path = tmp_path / "state" / "system.duckdb"
        self._create_v2_db(db_path)
        monkeypatch.setenv("DATA_DIR", str(tmp_path))

        conn = get_system_db()
        try:
            assert get_schema_version(conn) == SCHEMA_VERSION
        finally:
            conn.close()
            # Drop the cached connection so the snapshot file isn't
            # locked when we re-open it.
            from src import db as _db
            _db._system_db_conn = None
            _db._system_db_path = None

        snapshot = tmp_path / "state" / "system.duckdb.pre-migrate"
        assert snapshot.exists(), (
            "fixture precondition: snapshot must be written for a v2→vN "
            "migration"
        )

        import duckdb as _duckdb
        snap = _duckdb.connect(str(snapshot), read_only=True)
        try:
            tables_in_snapshot = {
                r[0]
                for r in snap.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'main'"
                ).fetchall()
            }
        finally:
            snap.close()

        # Tables NOT present in the v2 fixture but added by later
        # migrations (and therefore created by _SYSTEM_SCHEMA on the
        # modern binary). If any of these leaked into the snapshot, the
        # snapshot was contaminated by a self-heal pass running before
        # the snapshot copy.
        post_v2_tables = {
            "view_ownership",        # v10 (#100)
            "marketplace_registry",  # v11
            "marketplace_plugins",   # v11
            "user_groups",           # v11+ / v13
            "user_group_members",    # v13 (#106)
            "resource_grants",       # v13 (#106)
        }
        leaked = post_v2_tables & tables_in_snapshot
        assert not leaked, (
            f"pre-migration snapshot was contaminated with post-v2 "
            f"tables — self-heal pass ran before the snapshot copy. "
            f"Leaked: {sorted(leaked)}"
        )


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


class TestExtensionReattach:
    """Resilience tests for _reattach_remote_extensions() called by get_analytics_db_readonly()."""

    def _make_analytics_db(self, tmp_path):
        """Create an empty analytics server.duckdb so get_analytics_db_readonly() takes the read_only path."""
        analytics_dir = tmp_path / "analytics"
        analytics_dir.mkdir(parents=True, exist_ok=True)
        import duckdb as _duckdb
        conn = _duckdb.connect(str(analytics_dir / "server.duckdb"))
        conn.close()

    def _make_extract_db(self, tmp_path, source_name, with_remote_attach=True):
        """Create a minimal extract.duckdb, optionally with a _remote_attach table."""
        ext_dir = tmp_path / "extracts" / source_name
        ext_dir.mkdir(parents=True, exist_ok=True)
        import duckdb as _duckdb
        conn = _duckdb.connect(str(ext_dir / "extract.duckdb"))
        try:
            conn.execute(
                "CREATE TABLE _meta (table_name VARCHAR, description VARCHAR, rows BIGINT, "
                "size_bytes BIGINT, extracted_at TIMESTAMP, query_mode VARCHAR)"
            )
            if with_remote_attach:
                conn.execute(
                    "CREATE TABLE _remote_attach (alias VARCHAR, extension VARCHAR, url VARCHAR, token_env VARCHAR)"
                )
                # Use 'bigquery' which won't be installed in CI — tests resilience
                conn.execute(
                    "INSERT INTO _remote_attach VALUES ('bq', 'bigquery', 'project/dataset', '')"
                )
        finally:
            conn.close()

    def test_reads_remote_attach_table(self, tmp_path, monkeypatch):
        """get_analytics_db_readonly() doesn't crash even when LOAD fails for missing extension."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        import importlib
        import src.db as db_module
        importlib.reload(db_module)

        self._make_analytics_db(tmp_path)
        self._make_extract_db(tmp_path, "mysource", with_remote_attach=True)

        # Should not raise even though 'bigquery' extension is not installed
        conn = db_module.get_analytics_db_readonly()
        try:
            # Connection must still be usable for local queries
            result = conn.execute("SELECT 42 AS n").fetchone()
            assert result[0] == 42
        finally:
            conn.close()

    def test_reattach_attempts_load(self, tmp_path, monkeypatch):
        """Verify _reattach_remote_extensions reads _remote_attach and attempts LOAD."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        import importlib
        import src.db as db_module
        importlib.reload(db_module)

        self._make_analytics_db(tmp_path)
        self._make_extract_db(tmp_path, "bqsource", with_remote_attach=True)

        # Call get_analytics_db_readonly and verify the _remote_attach table is readable
        conn = db_module.get_analytics_db_readonly()
        try:
            # Verify the extract was attached
            dbs = {r[0] for r in conn.execute("SELECT database_name FROM duckdb_databases()").fetchall()}
            assert "bqsource" in dbs, f"bqsource should be attached, got: {dbs}"

            # Verify _remote_attach table is accessible via table_catalog
            has = conn.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_catalog='bqsource' AND table_name='_remote_attach'"
            ).fetchone()
            assert has is not None, "_remote_attach table should be visible via table_catalog"

            # Read the rows to verify they're correct
            rows = conn.execute(
                "SELECT alias, extension, url FROM bqsource._remote_attach"
            ).fetchall()
            assert len(rows) == 1
            assert rows[0][0] == "bq"
            assert rows[0][1] == "bigquery"
        finally:
            conn.close()

    def test_skips_missing_remote_attach(self, tmp_path, monkeypatch):
        """get_analytics_db_readonly() works fine when _remote_attach table is absent."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        import importlib
        import src.db as db_module
        importlib.reload(db_module)

        self._make_analytics_db(tmp_path)
        self._make_extract_db(tmp_path, "localsource", with_remote_attach=False)

        conn = db_module.get_analytics_db_readonly()
        try:
            result = conn.execute("SELECT 'ok' AS status").fetchone()
            assert result[0] == "ok"
        finally:
            conn.close()


class TestReattachRemoteExtensionsBQ:
    """src.db.get_analytics_db_readonly() / _reattach_remote_extensions must
    refresh BQ token from GCE metadata when extension='bigquery' (secret is
    session-scoped, so it has to be recreated on every readonly-conn open)."""

    def _make_analytics_db(self, tmp_path):
        analytics_dir = tmp_path / "analytics"
        analytics_dir.mkdir(parents=True, exist_ok=True)
        conn = duckdb.connect(str(analytics_dir / "server.duckdb"))
        conn.close()

    def _make_bq_extract(self, tmp_path, source_name):
        ext_dir = tmp_path / "extracts" / source_name
        ext_dir.mkdir(parents=True, exist_ok=True)
        conn = duckdb.connect(str(ext_dir / "extract.duckdb"))
        try:
            conn.execute(
                "CREATE TABLE _meta (table_name VARCHAR, description VARCHAR, "
                "rows BIGINT, size_bytes BIGINT, extracted_at TIMESTAMP, "
                "query_mode VARCHAR DEFAULT 'remote')"
            )
            conn.execute(
                "CREATE TABLE _remote_attach "
                "(alias VARCHAR, extension VARCHAR, url VARCHAR, token_env VARCHAR)"
            )
            conn.execute(
                "INSERT INTO _remote_attach VALUES "
                "('bq', 'bigquery', 'project=test-proj', '')"
            )
            # Co-located local stub table so the readonly conn has something usable.
            conn.execute('CREATE TABLE "stub" (x INT)')
            conn.execute("INSERT INTO stub VALUES (1)")
            conn.execute(
                "INSERT INTO _meta VALUES "
                "('stub', '', 1, 0, current_timestamp, 'local')"
            )
        finally:
            conn.close()

    def test_bq_reattach_calls_get_metadata_token(self, tmp_path, monkeypatch):
        """BQ row in _remote_attach triggers get_metadata_token() and a
        CREATE OR REPLACE SECRET for the alias."""
        from unittest.mock import MagicMock

        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        import importlib
        import src.db as db_module
        importlib.reload(db_module)

        self._make_analytics_db(tmp_path)
        self._make_bq_extract(tmp_path, "bigquery")

        called = {"count": 0}

        def fake_token():
            called["count"] += 1
            return "ya29.fake-token"

        monkeypatch.setattr(db_module, "get_metadata_token", fake_token)

        # Capture SQL on the readonly connection. DuckDB connections have
        # read-only attributes, so wrap in a proxy. Stub LOAD/SECRET/ATTACH
        # for BigQuery (TYPE bigquery) so we don't need real BQ network.
        captured = []

        class _ConnProxy:
            def __init__(self, inner):
                self._inner = inner

            def execute(self, sql, *args, **kwargs):
                captured.append(sql)
                up = sql.upper()
                if "LOAD BIGQUERY" in up:
                    return MagicMock()
                if "CREATE OR REPLACE SECRET" in up and "TYPE BIGQUERY" in up:
                    return MagicMock()
                if up.startswith("ATTACH ") and "TYPE BIGQUERY" in up:
                    return MagicMock()
                return self._inner.execute(sql, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(self._inner, name)

        real_connect = duckdb.connect

        def spy_connect(path, *a, **kw):
            return _ConnProxy(real_connect(path, *a, **kw))

        monkeypatch.setattr(db_module.duckdb, "connect", spy_connect)

        conn = db_module.get_analytics_db_readonly()
        try:
            assert called["count"] >= 1, \
                "get_metadata_token() must be called for BQ source"
            assert any(
                "CREATE OR REPLACE SECRET" in s.upper() and "TYPE BIGQUERY" in s.upper()
                for s in captured
            ), "must create DuckDB secret with metadata token"
            attach_for_bq = [
                s for s in captured
                if s.upper().startswith("ATTACH ") and "TYPE BIGQUERY" in s.upper()
            ]
            assert attach_for_bq, "expected ATTACH for the bq alias"
            assert all("TOKEN '" not in s for s in attach_for_bq), \
                f"BQ ATTACH must not pass TOKEN= directly; got: {attach_for_bq}"
        finally:
            conn.close()

    def test_bq_reattach_failure_logs_and_skips(self, tmp_path, monkeypatch):
        """If GCE metadata is unreachable, _reattach_remote_extensions logs an
        ERROR and skips ATTACH — connection is still usable for local queries.

        Uses a direct spy on ``logger.error`` instead of pytest's ``caplog``
        because ``caplog`` was unreliable in CI when combined with the
        ``importlib.reload(src.db)`` setup pattern (handler attachment / log-
        propagation timing differed from the local pytest config and yielded
        empty ``caplog.records``). A method-level spy is fully independent of
        pytest's logging plumbing.

        Also stubs ``LOAD bigquery`` (and friends) via a connection proxy
        because CI runners don't have the BigQuery community extension cached
        on disk. Without the stub, ``conn.execute("LOAD bigquery;")`` inside
        ``_reattach_remote_extensions`` raises a Catalog/IO/HTTP error before
        the BQ branch can run, the outer ``except`` swallows it at
        ``logger.debug``, and the BQ-specific ``logger.error`` we're asserting
        on is never reached. Mirrors the stub already present in
        ``test_bq_reattach_calls_get_metadata_token``.
        """
        from unittest.mock import MagicMock
        from connectors.bigquery.auth import BQMetadataAuthError

        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        import importlib
        import src.db as db_module
        importlib.reload(db_module)

        self._make_analytics_db(tmp_path)
        self._make_bq_extract(tmp_path, "bigquery")

        # Patch the local binding in src.db (NOT in connectors.bigquery.auth) —
        # `from connectors.bigquery.auth import get_metadata_token` creates a
        # fresh name in src.db's namespace; the call site looks it up there.
        def boom():
            raise BQMetadataAuthError("metadata server unreachable: simulated")
        monkeypatch.setattr("src.db.get_metadata_token", boom)

        # Stub BigQuery-extension-specific SQL on the readonly connection so the
        # test doesn't depend on the community extension being cached on disk
        # (CI runners start clean). With this stub, `LOAD bigquery` returns a
        # MagicMock and the BQ branch in _reattach_remote_extensions is reached;
        # `boom` then raises BQMetadataAuthError, which the production code
        # catches and logs at ERROR — exactly what the spy below verifies.
        class _ConnProxy:
            def __init__(self, inner):
                self._inner = inner

            def execute(self, sql, *args, **kwargs):
                up = sql.upper()
                if "LOAD BIGQUERY" in up or "INSTALL BIGQUERY" in up:
                    return MagicMock()
                if "CREATE OR REPLACE SECRET" in up and "TYPE BIGQUERY" in up:
                    return MagicMock()
                if up.startswith("ATTACH ") and "TYPE BIGQUERY" in up:
                    return MagicMock()
                return self._inner.execute(sql, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(self._inner, name)

        real_connect = duckdb.connect

        def spy_connect(path, *a, **kw):
            return _ConnProxy(real_connect(path, *a, **kw))

        monkeypatch.setattr(db_module.duckdb, "connect", spy_connect)

        # Direct spy on logger.error — captures regardless of pytest config /
        # propagation / handler attachment.
        captured_errors: list[str] = []
        real_error = db_module.logger.error

        def spy_error(msg, *args, **kwargs):
            try:
                formatted = msg % args if args else msg
            except (TypeError, ValueError):
                formatted = str(msg) + " | args=" + repr(args)
            captured_errors.append(formatted)
            return real_error(msg, *args, **kwargs)

        monkeypatch.setattr(db_module.logger, "error", spy_error)

        conn = db_module.get_analytics_db_readonly()
        try:
            # Connection still usable for local SQL — BQ failure didn't break it
            row = conn.execute("SELECT 7 AS n").fetchone()
            assert row[0] == 7
            assert any("metadata" in m.lower() for m in captured_errors), (
                f"expected ERROR log mentioning metadata; "
                f"got {len(captured_errors)} errors: {captured_errors}"
            )
        finally:
            conn.close()


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


class TestSchemaV12:
    """Tests for v12: user_group_members + resource_grants tables."""

    def test_user_group_members_table_exists(self, tmp_path, monkeypatch):
        _setup_data_dir(tmp_path, monkeypatch)
        from src.db import get_system_db

        conn = get_system_db()
        try:
            cols = {
                r[0]
                for r in conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='user_group_members'"
                ).fetchall()
            }
            assert {"user_id", "group_id", "source"} <= cols
        finally:
            conn.close()

    def test_resource_grants_table_exists(self, tmp_path, monkeypatch):
        _setup_data_dir(tmp_path, monkeypatch)
        from src.db import get_system_db

        conn = get_system_db()
        try:
            cols = {
                r[0]
                for r in conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='resource_grants'"
                ).fetchall()
            }
            assert {"id", "group_id", "resource_type", "resource_id"} <= cols
        finally:
            conn.close()

    def test_admin_and_everyone_seeded(self, tmp_path, monkeypatch):
        _setup_data_dir(tmp_path, monkeypatch)
        from src.db import get_system_db

        conn = get_system_db()
        try:
            rows = {
                r[0]: r[1] for r in conn.execute(
                    "SELECT name, is_system FROM user_groups"
                ).fetchall()
            }
            assert rows.get("Admin") is True
            assert rows.get("Everyone") is True
        finally:
            conn.close()

    def test_legacy_tables_dropped(self, tmp_path, monkeypatch):
        _setup_data_dir(tmp_path, monkeypatch)
        from src.db import get_system_db

        conn = get_system_db()
        try:
            existing = {
                r[0] for r in conn.execute(
                    "SELECT table_name FROM information_schema.tables"
                ).fetchall()
            }
            for legacy in ("internal_roles", "group_mappings", "user_role_grants", "plugin_access"):
                assert legacy not in existing, f"{legacy} should have been dropped in v13"
        finally:
            conn.close()

    def test_v12_to_v13_migration_backfill(self, tmp_path, monkeypatch):
        """A v12 DB with sample data is fully migrated and backfilled to v13."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        import json
        import uuid
        import duckdb as _duckdb
        from src.db import get_system_db, get_schema_version, SCHEMA_VERSION

        db_path = tmp_path / "state" / "system.duckdb"
        db_path.parent.mkdir(parents=True, exist_ok=True)

        # Build a minimal v12 schema by hand (users.groups JSON + is_system
        # already in place, RBAC collapse not yet done).
        conn = _duckdb.connect(str(db_path))
        conn.execute("""
            CREATE TABLE schema_version (version INTEGER, applied_at TIMESTAMP DEFAULT current_timestamp);
            INSERT INTO schema_version (version) VALUES (12);
            CREATE TABLE users (
                id VARCHAR PRIMARY KEY, email VARCHAR UNIQUE NOT NULL, name VARCHAR, role VARCHAR,
                password_hash VARCHAR, setup_token VARCHAR, setup_token_created TIMESTAMP,
                reset_token VARCHAR, reset_token_created TIMESTAMP,
                active BOOLEAN DEFAULT TRUE, deactivated_at TIMESTAMP, deactivated_by VARCHAR,
                groups JSON, created_at TIMESTAMP, updated_at TIMESTAMP
            );
            CREATE TABLE internal_roles (id VARCHAR PRIMARY KEY, key VARCHAR UNIQUE NOT NULL,
                display_name VARCHAR NOT NULL, description TEXT, owner_module VARCHAR,
                implies VARCHAR, is_core BOOLEAN, created_at TIMESTAMP, updated_at TIMESTAMP);
            CREATE TABLE user_role_grants (id VARCHAR PRIMARY KEY,
                user_id VARCHAR REFERENCES users(id),
                internal_role_id VARCHAR REFERENCES internal_roles(id),
                granted_at TIMESTAMP, granted_by VARCHAR, source VARCHAR);
            CREATE TABLE group_mappings (id VARCHAR PRIMARY KEY, external_group_id VARCHAR,
                internal_role_id VARCHAR REFERENCES internal_roles(id),
                assigned_at TIMESTAMP, assigned_by VARCHAR);
            CREATE TABLE user_groups (id VARCHAR PRIMARY KEY, name VARCHAR UNIQUE,
                description TEXT, is_system BOOLEAN, created_at TIMESTAMP, created_by VARCHAR);
            CREATE TABLE plugin_access (group_id VARCHAR, marketplace_id VARCHAR,
                plugin_name VARCHAR, granted_at TIMESTAMP, granted_by VARCHAR);
        """)
        admin_uid = str(uuid.uuid4())
        bob_uid = str(uuid.uuid4())
        conn.execute("INSERT INTO users (id, email, name, groups) VALUES (?, ?, ?, ?), (?, ?, ?, ?)",
            [admin_uid, 'admin@x', 'A', json.dumps(['Engineering']),
             bob_uid, 'bob@x', 'B', None])
        eng_id = str(uuid.uuid4())
        conn.execute("INSERT INTO user_groups (id, name) VALUES (?, ?)", [eng_id, 'Engineering'])
        # core.admin grant on admin
        core_admin = str(uuid.uuid4())
        conn.execute("INSERT INTO internal_roles (id, key, display_name) VALUES (?, 'core.admin', 'Admin')",
            [core_admin])
        conn.execute("INSERT INTO user_role_grants (id, user_id, internal_role_id) VALUES (?, ?, ?)",
            [str(uuid.uuid4()), admin_uid, core_admin])
        conn.execute("INSERT INTO plugin_access (group_id, marketplace_id, plugin_name) VALUES (?, ?, ?)",
            [eng_id, 'foundry-ai', 'metrics'])
        conn.close()

        # Trigger upgrade.
        conn = get_system_db()
        try:
            assert get_schema_version(conn) == SCHEMA_VERSION

            # admin → Admin + Engineering + Everyone
            admin_groups = {
                r[0] for r in conn.execute(
                    """SELECT g.name FROM user_group_members m
                       JOIN user_groups g ON g.id = m.group_id
                       WHERE m.user_id = ?""", [admin_uid]
                ).fetchall()
            }
            assert {"Admin", "Engineering", "Everyone"} <= admin_groups

            # bob → only Everyone
            bob_groups = {
                r[0] for r in conn.execute(
                    """SELECT g.name FROM user_group_members m
                       JOIN user_groups g ON g.id = m.group_id
                       WHERE m.user_id = ?""", [bob_uid]
                ).fetchall()
            }
            assert bob_groups == {"Everyone"}

            # plugin_access → resource_grants
            grants = conn.execute(
                """SELECT resource_type, resource_id FROM resource_grants
                   WHERE group_id = ?""", [eng_id]
            ).fetchall()
            assert grants == [("marketplace_plugin", "foundry-ai/metrics")]
        finally:
            conn.close()

    def test_v12_to_v13_finalize_rollback_on_failure(self, tmp_path, monkeypatch):
        """Mid-flight failure in _v12_to_v13_finalize rolls the v13 backfill
        back to a clean v12 state and the next start retries the migration.

        Setup mirrors test_v12_to_v13_migration_backfill — a hand-crafted v12
        DB with sample data that the finalize would otherwise migrate. We
        monkey-patch _seed_system_groups (the first call inside the
        transaction) to raise mid-finalize and verify:

            1. schema_version stays at 12.
            2. Legacy tables (user_role_grants, plugin_access, …) are NOT
               dropped — the finalize had not reached the DROP step.
            3. user_group_members + resource_grants are EMPTY (the inserts
               that ran before the failure were rolled back).
            4. A second start succeeds and produces the same final state as
               a clean run.
        """
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        import json
        import uuid
        import duckdb as _duckdb
        from src import db as _db
        from src.db import get_system_db, get_schema_version, SCHEMA_VERSION

        db_path = tmp_path / "state" / "system.duckdb"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = _duckdb.connect(str(db_path))
        conn.execute("""
            CREATE TABLE schema_version (version INTEGER, applied_at TIMESTAMP DEFAULT current_timestamp);
            INSERT INTO schema_version (version) VALUES (12);
            CREATE TABLE users (
                id VARCHAR PRIMARY KEY, email VARCHAR UNIQUE NOT NULL, name VARCHAR, role VARCHAR,
                password_hash VARCHAR, setup_token VARCHAR, setup_token_created TIMESTAMP,
                reset_token VARCHAR, reset_token_created TIMESTAMP,
                active BOOLEAN DEFAULT TRUE, deactivated_at TIMESTAMP, deactivated_by VARCHAR,
                groups JSON, created_at TIMESTAMP, updated_at TIMESTAMP
            );
            CREATE TABLE internal_roles (id VARCHAR PRIMARY KEY, key VARCHAR UNIQUE NOT NULL,
                display_name VARCHAR NOT NULL, description TEXT, owner_module VARCHAR,
                implies VARCHAR, is_core BOOLEAN, created_at TIMESTAMP, updated_at TIMESTAMP);
            CREATE TABLE user_role_grants (id VARCHAR PRIMARY KEY,
                user_id VARCHAR REFERENCES users(id),
                internal_role_id VARCHAR REFERENCES internal_roles(id),
                granted_at TIMESTAMP, granted_by VARCHAR, source VARCHAR);
            CREATE TABLE group_mappings (id VARCHAR PRIMARY KEY, external_group_id VARCHAR,
                internal_role_id VARCHAR REFERENCES internal_roles(id),
                assigned_at TIMESTAMP, assigned_by VARCHAR);
            CREATE TABLE user_groups (id VARCHAR PRIMARY KEY, name VARCHAR UNIQUE,
                description TEXT, is_system BOOLEAN, created_at TIMESTAMP, created_by VARCHAR);
            CREATE TABLE plugin_access (group_id VARCHAR, marketplace_id VARCHAR,
                plugin_name VARCHAR, granted_at TIMESTAMP, granted_by VARCHAR);
        """)
        admin_uid = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO users (id, email, name, groups) VALUES (?, ?, ?, ?)",
            [admin_uid, 'admin@x', 'A', json.dumps(['Engineering'])],
        )
        eng_id = str(uuid.uuid4())
        conn.execute("INSERT INTO user_groups (id, name) VALUES (?, ?)", [eng_id, 'Engineering'])
        core_admin = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO internal_roles (id, key, display_name) VALUES (?, 'core.admin', 'Admin')",
            [core_admin],
        )
        conn.execute(
            "INSERT INTO user_role_grants (id, user_id, internal_role_id) VALUES (?, ?, ?)",
            [str(uuid.uuid4()), admin_uid, core_admin],
        )
        conn.execute(
            "INSERT INTO plugin_access (group_id, marketplace_id, plugin_name) VALUES (?, ?, ?)",
            [eng_id, 'foundry-ai', 'metrics'],
        )
        conn.close()

        # Inject a failure inside the v12→v13 finalize transaction.
        original_seed = _db._seed_system_groups
        def _boom(_conn):
            raise RuntimeError("synthetic mid-flight failure")
        monkeypatch.setattr(_db, "_seed_system_groups", _boom)

        with pytest.raises(RuntimeError, match="synthetic mid-flight failure"):
            get_system_db()
        # Drop the cached connection the failed _ensure_schema may have
        # registered (its lock is held; we want a clean re-attempt below).
        _db._system_db_conn = None

        # Open the DB raw and verify rollback.
        conn = _duckdb.connect(str(db_path))
        try:
            assert get_schema_version(conn) == 12, (
                "schema_version must stay at 12 after rollback"
            )
            tables = {
                r[0] for r in conn.execute(
                    "SELECT table_name FROM information_schema.tables"
                ).fetchall()
            }
            for legacy in ("internal_roles", "group_mappings",
                           "user_role_grants", "plugin_access"):
                assert legacy in tables, (
                    f"{legacy} must NOT be dropped on rollback"
                )
            # New tables exist (created by _V12_TO_V13_MIGRATIONS before the
            # finalize ran) but contain no rows.
            assert tables.issuperset({"user_group_members", "resource_grants"})
            count_members = conn.execute(
                "SELECT COUNT(*) FROM user_group_members"
            ).fetchone()[0]
            count_grants = conn.execute(
                "SELECT COUNT(*) FROM resource_grants"
            ).fetchone()[0]
            assert count_members == 0, "backfill rows leaked past ROLLBACK"
            assert count_grants == 0, "backfill rows leaked past ROLLBACK"
        finally:
            conn.close()

        # Restore the real finalize and verify a fresh start completes.
        monkeypatch.setattr(_db, "_seed_system_groups", original_seed)
        conn = get_system_db()
        try:
            assert get_schema_version(conn) == SCHEMA_VERSION
            count_members = conn.execute(
                "SELECT COUNT(*) FROM user_group_members"
            ).fetchone()[0]
            assert count_members > 0, "retry should backfill members"
        finally:
            conn.close()


class TestV13ToV14Migration:
    """Tests for v13→v14 finalize: orphan cleanup + FK constraints + rollback."""

    def _create_v13_db(self, tmp_path, monkeypatch):
        """Create a v13 database with some data including orphan records."""
        import json
        import uuid
        import duckdb as _duckdb

        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        db_path = tmp_path / "state" / "system.duckdb"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = _duckdb.connect(str(db_path))

        # Build a minimal v13 schema
        conn.execute("""
            CREATE TABLE schema_version (version INTEGER, applied_at TIMESTAMP DEFAULT current_timestamp);
            INSERT INTO schema_version (version) VALUES (13);
            CREATE TABLE users (
                id VARCHAR PRIMARY KEY, email VARCHAR UNIQUE NOT NULL, name VARCHAR, role VARCHAR,
                password_hash VARCHAR, setup_token VARCHAR, setup_token_created TIMESTAMP,
                reset_token VARCHAR, reset_token_created TIMESTAMP,
                active BOOLEAN DEFAULT TRUE, deactivated_at TIMESTAMP, deactivated_by VARCHAR,
                created_at TIMESTAMP, updated_at TIMESTAMP
            );
            CREATE TABLE user_groups (
                id VARCHAR PRIMARY KEY, name VARCHAR UNIQUE,
                description TEXT, is_system BOOLEAN, created_at TIMESTAMP, created_by VARCHAR
            );
            CREATE TABLE user_group_members (
                id VARCHAR PRIMARY KEY, user_id VARCHAR, group_id VARCHAR,
                source VARCHAR, added_at TIMESTAMP, added_by VARCHAR
            );
            CREATE TABLE resource_grants (
                id VARCHAR PRIMARY KEY, group_id VARCHAR,
                resource_type VARCHAR, resource_id VARCHAR,
                assigned_at TIMESTAMP, assigned_by VARCHAR
            );
            CREATE TABLE table_registry (
                id VARCHAR PRIMARY KEY, name VARCHAR, source_type VARCHAR, bucket VARCHAR,
                source_table VARCHAR, query_mode VARCHAR, sync_schedule VARCHAR,
                profile_after_sync BOOLEAN, is_public BOOLEAN, description TEXT,
                created_at TIMESTAMP, updated_at TIMESTAMP
            );
            CREATE TABLE sync_state (table_id VARCHAR PRIMARY KEY, status VARCHAR,
                last_sync TIMESTAMP, rows INTEGER, size_bytes INTEGER, error TEXT);
            CREATE TABLE sync_history (id VARCHAR PRIMARY KEY, table_id VARCHAR,
                status VARCHAR, started_at TIMESTAMP, finished_at TIMESTAMP,
                rows INTEGER, size_bytes INTEGER, error TEXT);
            CREATE TABLE personal_access_tokens (
                id VARCHAR PRIMARY KEY, user_id VARCHAR, name VARCHAR,
                token_hash VARCHAR, prefix VARCHAR, scopes VARCHAR,
                created_at TIMESTAMP, expires_at TIMESTAMP,
                last_used_at TIMESTAMP, last_used_ip VARCHAR, revoked_at TIMESTAMP
            );
            CREATE TABLE view_ownership (
                source_name VARCHAR, table_name VARCHAR, owner_id VARCHAR,
                claimed_at TIMESTAMP DEFAULT current_timestamp,
                PRIMARY KEY (source_name, table_name)
            );
        """)

        # Seed system groups
        admin_gid = str(uuid.uuid4())
        everyone_gid = str(uuid.uuid4())
        conn.execute("INSERT INTO user_groups (id, name, is_system) VALUES (?, 'Admin', TRUE)", [admin_gid])
        conn.execute("INSERT INTO user_groups (id, name, is_system) VALUES (?, 'Everyone', TRUE)", [everyone_gid])

        # Seed a user
        uid = str(uuid.uuid4())
        conn.execute("INSERT INTO users (id, email, name, role) VALUES (?, 'test@x.com', 'Test', 'analyst')", [uid])

        # Valid memberships
        conn.execute(
            "INSERT INTO user_group_members (id, user_id, group_id, source, added_at, added_by) VALUES (?, ?, ?, 'admin', current_timestamp, 'admin')",
            [str(uuid.uuid4()), uid, everyone_gid],
        )

        # Orphan: membership referencing non-existent group (FK target missing)
        orphan_mid = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO user_group_members (id, user_id, group_id, source, added_at, added_by) VALUES (?, ?, 'nonexistent-group', 'admin', current_timestamp, 'admin')",
            [orphan_mid, uid],
        )

        # Orphan: grant referencing non-existent group
        orphan_gid = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO resource_grants (id, group_id, resource_type, resource_id, assigned_at, assigned_by) VALUES (?, ?, 'plugin', 'test-plugin', current_timestamp, 'admin')",
            [orphan_gid, 'nonexistent-group'],
        )

        # Valid grant
        conn.execute(
            "INSERT INTO resource_grants (id, group_id, resource_type, resource_id, assigned_at, assigned_by) VALUES (?, ?, 'plugin', 'valid-plugin', current_timestamp, 'admin')",
            [str(uuid.uuid4()), everyone_gid],
        )

        conn.close()
        return db_path, uid, admin_gid, everyone_gid, orphan_mid

    def test_v13_to_v14_orphan_cleanup(self, tmp_path, monkeypatch):
        """v13→v14 finalize must clean up orphan records before adding FK constraints."""
        db_path, uid, admin_gid, everyone_gid, orphan_mid = self._create_v13_db(tmp_path, monkeypatch)
        from src.db import get_system_db, get_schema_version, SCHEMA_VERSION

        conn = get_system_db()
        try:
            assert get_schema_version(conn) == SCHEMA_VERSION

            # Orphan membership should have been deleted
            orphans = conn.execute(
                "SELECT COUNT(*) FROM user_group_members WHERE group_id = 'nonexistent-group'"
            ).fetchone()[0]
            assert orphans == 0, "orphan user_group_members should be cleaned up"

            # Orphan grant should have been deleted
            orphan_grants = conn.execute(
                "SELECT COUNT(*) FROM resource_grants WHERE group_id = 'nonexistent-group'"
            ).fetchone()[0]
            assert orphan_grants == 0, "orphan resource_grants should be cleaned up"

            # Valid records should still exist
            valid_members = conn.execute(
                "SELECT COUNT(*) FROM user_group_members WHERE user_id = ?", [uid]
            ).fetchone()[0]
            assert valid_members > 0, "valid memberships should be preserved"

            valid_grants = conn.execute(
                "SELECT COUNT(*) FROM resource_grants WHERE group_id = ?", [everyone_gid]
            ).fetchone()[0]
            assert valid_grants > 0, "valid grants should be preserved"
        finally:
            conn.close()

    def test_v13_to_v14_fk_constraints_added(self, tmp_path, monkeypatch):
        """v13→v14 finalize must add FK constraints on user_group_members and resource_grants."""
        db_path, *_ = self._create_v13_db(tmp_path, monkeypatch)
        import duckdb as _duckdb
        from src.db import get_system_db

        conn = get_system_db()
        try:
            # Check FK constraints exist on user_group_members
            fks_members = conn.execute(
                "SELECT constraint_text FROM duckdb_constraints() "
                "WHERE table_name = 'user_group_members' AND constraint_type = 'FOREIGN KEY'"
            ).fetchall()
            fk_texts = [fk[0] for fk in fks_members]
            assert any('user_groups' in t for t in fk_texts), "FK to user_groups should exist on user_group_members"

            # Check FK constraints exist on resource_grants
            fks_grants = conn.execute(
                "SELECT constraint_text FROM duckdb_constraints() "
                "WHERE table_name = 'resource_grants' AND constraint_type = 'FOREIGN KEY'"
            ).fetchall()
            fk_texts_g = [fk[0] for fk in fks_grants]
            assert any('user_groups' in t for t in fk_texts_g), "FK to user_groups should exist on resource_grants"
        finally:
            conn.close()

    def test_v13_to_v14_rollback_on_failure(self, tmp_path, monkeypatch):
        """If v13→v14 finalize fails, schema_version must stay at 13 and rollback."""
        db_path, *_ = self._create_v13_db(tmp_path, monkeypatch)
        from src import db as _db
        from src.db import get_system_db, get_schema_version

        # Inject a failure inside the v13→v14 finalize
        original_finalize = _db._v13_to_v14_finalize
        def _boom(_conn):
            raise RuntimeError("synthetic v14 finalize failure")
        monkeypatch.setattr(_db, "_v13_to_v14_finalize", _boom)

        with pytest.raises(RuntimeError, match="synthetic v14 finalize failure"):
            get_system_db()
        _db._system_db_conn = None

        # Verify rollback: schema_version still 13
        import duckdb as _duckdb
        conn = _duckdb.connect(str(db_path))
        try:
            assert get_schema_version(conn) == 13, "schema_version must stay at 13 after rollback"
        finally:
            conn.close()

        # Restore and retry — should succeed
        monkeypatch.setattr(_db, "_v13_to_v14_finalize", original_finalize)
        conn = get_system_db()
        try:
            from src.db import SCHEMA_VERSION
            assert get_schema_version(conn) == SCHEMA_VERSION
        finally:
            conn.close()
