"""v26 migration: instance_templates consolidation + users.onboarded.

Consolidates the v21 welcome_template and v23 claude_md_template singletons
into a generic instance_templates(key, content, ...) table. Adds users.onboarded
boolean for the /home state-aware landing page (default FALSE, explicit signal
required to flip).
"""
import duckdb

from src.db import SCHEMA_VERSION, _ensure_schema, get_schema_version


def test_schema_version_is_26():
    """v26 = home page (instance_templates consolidation + users.onboarded)."""
    assert SCHEMA_VERSION == 26


def test_v26_creates_instance_templates(tmp_path):
    """Fresh install at v26 creates instance_templates with three seeded keys."""
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)

    tables = {
        r[0]
        for r in conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main'"
        ).fetchall()
    }
    assert "instance_templates" in tables, f"instance_templates missing from {tables}"

    rows = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT key, content FROM instance_templates ORDER BY key"
        ).fetchall()
    }
    assert set(rows.keys()) == {"welcome", "claude_md", "home"}
    assert rows["home"] is None
    assert rows["welcome"] is None
    assert rows["claude_md"] is None
    conn.close()


def test_v26_drops_legacy_template_tables(tmp_path):
    """Fresh install at v26 does NOT have welcome_template or claude_md_template
    as separate tables — they're consolidated into instance_templates."""
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)

    tables = {
        r[0]
        for r in conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main'"
        ).fetchall()
    }
    assert "welcome_template" not in tables, (
        f"welcome_template should be dropped post-v26, found in {tables}"
    )
    assert "claude_md_template" not in tables, (
        f"claude_md_template should be dropped post-v26, found in {tables}"
    )
    # setup_banner (v22 reserved) stays as compat per brainstorm decision
    assert "setup_banner" in tables
    conn.close()


def test_v26_users_onboarded_column(tmp_path):
    """v26 adds users.onboarded BOOLEAN DEFAULT FALSE."""
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)

    cols = {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'users'"
        ).fetchall()
    }
    assert "onboarded" in cols, f"users.onboarded missing from {cols}"

    # Insert a user, verify default FALSE
    conn.execute(
        "INSERT INTO users (id, email) VALUES ('u1', 'a@example.com')"
    )
    row = conn.execute("SELECT onboarded FROM users WHERE id = 'u1'").fetchone()
    assert row[0] is False, "users.onboarded should default to FALSE for new users"
    conn.close()


def test_v25_db_migrates_to_v26_preserving_template_content(tmp_path):
    """A v25 DB with content in welcome_template + claude_md_template upgrades
    cleanly: rows land in instance_templates, old tables dropped, content
    preserved."""
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))

    # Build a minimal v25-shaped DB with the two singletons populated.
    conn.execute(
        "CREATE TABLE schema_version (version INTEGER, "
        "applied_at TIMESTAMP DEFAULT current_timestamp)"
    )
    conn.execute("INSERT INTO schema_version (version) VALUES (25)")
    conn.execute(
        """CREATE TABLE users (
            id VARCHAR PRIMARY KEY, email VARCHAR UNIQUE NOT NULL,
            name VARCHAR, password_hash VARCHAR,
            setup_token VARCHAR, setup_token_created TIMESTAMP,
            reset_token VARCHAR, reset_token_created TIMESTAMP,
            active BOOLEAN NOT NULL DEFAULT TRUE,
            deactivated_at TIMESTAMP, deactivated_by VARCHAR,
            created_at TIMESTAMP DEFAULT current_timestamp,
            updated_at TIMESTAMP
        )"""
    )
    conn.execute(
        "INSERT INTO users (id, email) VALUES ('legacy_user', 'legacy@example.com')"
    )
    conn.execute(
        """CREATE TABLE welcome_template (
            id INTEGER PRIMARY KEY DEFAULT 1, content TEXT,
            updated_at TIMESTAMP, updated_by VARCHAR,
            CONSTRAINT singleton CHECK (id = 1)
        )"""
    )
    conn.execute(
        "INSERT INTO welcome_template (id, content, updated_by) "
        "VALUES (1, '<p>legacy welcome</p>', 'admin@example.com')"
    )
    conn.execute(
        """CREATE TABLE claude_md_template (
            id INTEGER PRIMARY KEY DEFAULT 1, content TEXT,
            updated_at TIMESTAMP, updated_by VARCHAR,
            CONSTRAINT singleton CHECK (id = 1)
        )"""
    )
    conn.execute(
        "INSERT INTO claude_md_template (id, content, updated_by) "
        "VALUES (1, '# legacy claude md', 'admin@example.com')"
    )

    _ensure_schema(conn)

    assert get_schema_version(conn) == 26

    rows = {
        row[0]: row
        for row in conn.execute(
            "SELECT key, content, updated_by FROM instance_templates"
        ).fetchall()
    }
    assert rows["welcome"][1] == "<p>legacy welcome</p>"
    assert rows["welcome"][2] == "admin@example.com"
    assert rows["claude_md"][1] == "# legacy claude md"
    assert rows["claude_md"][2] == "admin@example.com"
    assert rows["home"][1] is None  # newly seeded, never had a legacy source

    # Old tables gone
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main'"
        ).fetchall()
    }
    assert "welcome_template" not in tables
    assert "claude_md_template" not in tables

    # Existing user backfilled to FALSE per Decision §2 (no PAT-heuristic auto-flip)
    onboarded = conn.execute(
        "SELECT onboarded FROM users WHERE id = 'legacy_user'"
    ).fetchone()
    assert onboarded[0] is False
    conn.close()


def test_v26_migration_idempotent(tmp_path):
    """Running _ensure_schema twice on a fresh DB is a no-op (no duplicate rows,
    no errors)."""
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)
    _ensure_schema(conn)  # second pass, no error

    count = conn.execute("SELECT COUNT(*) FROM instance_templates").fetchone()[0]
    assert count == 3
    conn.close()
