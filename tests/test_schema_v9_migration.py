"""Tests for the v8→v9 schema migration: user_role_grants, internal_roles
extensions, core.* seed, and the legacy users.role backfill.

Tests are scenario-shaped: each one stands up a synthetic v8 DB (or a fresh
DB at v9) and asserts the post-migration state. All run via the same
_ensure_schema entry point that production uses on first connect, so a
green test means the on-disk migration also works.
"""

import json
import os
import uuid

import duckdb
import pytest


@pytest.fixture
def fresh_data_dir(tmp_path, monkeypatch):
    """Isolate DATA_DIR per test so the module-level connection cache in
    src.db doesn't leak v8 state between tests."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    # Force the module to release any cached connection from a previous test.
    import src.db as _db
    if _db._system_db_conn is not None:
        try:
            _db._system_db_conn.close()
        except Exception:
            pass
        _db._system_db_conn = None
        _db._system_db_path = None
    yield tmp_path


def _v8_state(db_path) -> duckdb.DuckDBPyConnection:
    """Hand-craft a minimal v8 DB so the v8→v9 migration has something to
    operate on. Mirrors only the tables the migration touches."""
    os.makedirs(os.path.dirname(str(db_path)), exist_ok=True)
    conn = duckdb.connect(str(db_path))
    conn.execute(
        "CREATE TABLE schema_version "
        "(version INTEGER, applied_at TIMESTAMP DEFAULT current_timestamp)"
    )
    conn.execute("INSERT INTO schema_version (version) VALUES (8)")
    conn.execute(
        """CREATE TABLE users (
            id VARCHAR PRIMARY KEY,
            email VARCHAR UNIQUE NOT NULL,
            name VARCHAR,
            role VARCHAR DEFAULT 'analyst',
            active BOOLEAN DEFAULT TRUE
        )"""
    )
    conn.execute(
        """CREATE TABLE internal_roles (
            id VARCHAR PRIMARY KEY,
            key VARCHAR UNIQUE NOT NULL,
            display_name VARCHAR NOT NULL,
            description TEXT,
            owner_module VARCHAR,
            created_at TIMESTAMP DEFAULT current_timestamp,
            updated_at TIMESTAMP DEFAULT current_timestamp
        )"""
    )
    conn.execute(
        """CREATE TABLE group_mappings (
            id VARCHAR PRIMARY KEY,
            external_group_id VARCHAR NOT NULL,
            internal_role_id VARCHAR NOT NULL,
            assigned_at TIMESTAMP DEFAULT current_timestamp,
            assigned_by VARCHAR
        )"""
    )
    return conn


class TestFreshInstall:
    """Fresh DB → v9 directly via _SYSTEM_SCHEMA + INSERT version + seed."""

    def test_schema_version_is_9(self, fresh_data_dir):
        from src.db import get_system_db, get_schema_version
        conn = get_system_db()
        assert get_schema_version(conn) == 9

    def test_core_roles_seeded_with_implies_hierarchy(self, fresh_data_dir):
        from src.db import get_system_db
        conn = get_system_db()
        rows = {
            r[0]: (r[1], r[2], r[3])
            for r in conn.execute(
                "SELECT key, display_name, implies, is_core "
                "FROM internal_roles WHERE is_core = true ORDER BY key"
            ).fetchall()
        }
        # All four core.* roles seeded.
        assert set(rows.keys()) == {
            "core.admin", "core.analyst", "core.km_admin", "core.viewer",
        }
        # Implies chain: admin → km_admin → analyst → viewer.
        assert json.loads(rows["core.admin"][1]) == ["core.km_admin"]
        assert json.loads(rows["core.km_admin"][1]) == ["core.analyst"]
        assert json.loads(rows["core.analyst"][1]) == ["core.viewer"]
        assert json.loads(rows["core.viewer"][1]) == []
        # is_core flag is True on all four.
        for key in rows:
            assert rows[key][2] is True, f"{key} should have is_core=true"

    def test_user_role_grants_table_exists_and_empty(self, fresh_data_dir):
        from src.db import get_system_db
        conn = get_system_db()
        # Smoke-query — table exists.
        result = conn.execute(
            "SELECT COUNT(*) FROM user_role_grants"
        ).fetchone()
        assert result[0] == 0


class TestV8ToV9Migration:
    """Existing v8 DB with users.role values → v9 backfill assertions."""

    def test_backfills_user_role_grants_from_legacy_role(self, fresh_data_dir):
        db_path = fresh_data_dir / "state" / "system.duckdb"
        conn = _v8_state(db_path)
        # Seed one user per legacy role.
        for role_str in ("viewer", "analyst", "km_admin", "admin"):
            conn.execute(
                "INSERT INTO users (id, email, role) VALUES (?, ?, ?)",
                [str(uuid.uuid4()), f"{role_str}@example.com", role_str],
            )
        conn.close()

        # Trigger migration.
        from src.db import get_system_db, get_schema_version
        conn = get_system_db()
        assert get_schema_version(conn) == 9

        rows = conn.execute(
            """SELECT u.email, r.key, g.source
               FROM users u
               JOIN user_role_grants g ON g.user_id = u.id
               JOIN internal_roles r ON g.internal_role_id = r.id
               ORDER BY u.email"""
        ).fetchall()
        assert rows == [
            ("admin@example.com", "core.admin", "auto-seed"),
            ("analyst@example.com", "core.analyst", "auto-seed"),
            ("km_admin@example.com", "core.km_admin", "auto-seed"),
            ("viewer@example.com", "core.viewer", "auto-seed"),
        ]

    def test_legacy_role_column_nulled_after_migration(self, fresh_data_dir):
        db_path = fresh_data_dir / "state" / "system.duckdb"
        conn = _v8_state(db_path)
        conn.execute(
            "INSERT INTO users (id, email, role) VALUES (?, ?, ?)",
            [str(uuid.uuid4()), "admin@example.com", "admin"],
        )
        conn.close()

        from src.db import get_system_db
        conn = get_system_db()
        # users.role column still exists (DuckDB FK blocks DROP) but is NULL.
        cols = [
            r[0] for r in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'users'"
            ).fetchall()
        ]
        assert "role" in cols, "legacy column kept as deprecated artifact"

        result = conn.execute(
            "SELECT role FROM users WHERE email = 'admin@example.com'"
        ).fetchone()
        assert result[0] is None, "legacy role value NULL-ed by v9 migration"

    def test_unknown_legacy_role_falls_back_to_viewer(self, fresh_data_dir):
        """A user with users.role='custom_thing' (not in the legacy enum)
        should still get a grant — fall back to core.viewer rather than
        leaving them ungranted."""
        db_path = fresh_data_dir / "state" / "system.duckdb"
        conn = _v8_state(db_path)
        conn.execute(
            "INSERT INTO users (id, email, role) VALUES (?, ?, ?)",
            [str(uuid.uuid4()), "weird@example.com", "custom_thing"],
        )
        conn.close()

        from src.db import get_system_db
        conn = get_system_db()
        result = conn.execute(
            """SELECT r.key
               FROM user_role_grants g
               JOIN internal_roles r ON g.internal_role_id = r.id
               JOIN users u ON g.user_id = u.id
               WHERE u.email = 'weird@example.com'"""
        ).fetchone()
        assert result == ("core.viewer",)


class TestImpliesEndpointsExist:
    """Sanity checks that the new schema columns are usable by other modules."""

    def test_internal_roles_has_implies_and_is_core(self, fresh_data_dir):
        from src.db import get_system_db
        conn = get_system_db()
        cols = [
            r[0] for r in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'internal_roles'"
            ).fetchall()
        ]
        assert "implies" in cols
        assert "is_core" in cols

    def test_user_role_grants_unique_constraint_holds(self, fresh_data_dir):
        """(user_id, internal_role_id) uniqueness — second INSERT raises."""
        from src.db import get_system_db
        from src.repositories.users import UserRepository
        from src.repositories.internal_roles import InternalRolesRepository

        conn = get_system_db()
        user_id = str(uuid.uuid4())
        UserRepository(conn).create(
            id=user_id, email="dup@example.com", name="Dup", role="analyst",
        )
        # create() already inserted a grant for core.analyst — the duplicate
        # explicit insert below must raise.
        analyst = InternalRolesRepository(conn).get_by_key("core.analyst")
        with pytest.raises(duckdb.ConstraintException):
            conn.execute(
                """INSERT INTO user_role_grants
                   (id, user_id, internal_role_id, granted_by, source)
                   VALUES (?, ?, ?, 'test', 'direct')""",
                [str(uuid.uuid4()), user_id, analyst["id"]],
            )
