"""v45 → v46 migration: per-user opt-out (dismiss) for curated memory items.

Adds ``knowledge_item_user_dismissed`` ((user_id, item_id) PK,
dismissed_at) plus an index on ``user_id`` to support the EXISTS subquery
used by list_items / search / count_items / bundle. Mandatory items are
governance-protected: the API rejects POSTs against them, and the SQL
filter exempts ``status = 'mandatory'`` so any stale row from before an
item was mandated is silently ignored.
"""

import duckdb

from src.db import SCHEMA_VERSION, _ensure_schema, _v45_to_v46, get_schema_version


def test_schema_version_is_46():
    assert SCHEMA_VERSION == 49


def test_fresh_install_creates_dismissed_table(tmp_path):
    """A brand-new DB ends at v46 with the dismiss table + index in place."""
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)

    assert get_schema_version(conn) == SCHEMA_VERSION

    tables = {
        r[0]
        for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchall()
    }
    assert "knowledge_item_user_dismissed" in tables, (
        f"knowledge_item_user_dismissed missing from {tables}"
    )

    cols = {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'knowledge_item_user_dismissed'"
        ).fetchall()
    }
    assert {"user_id", "item_id", "dismissed_at"} <= cols, (
        f"missing columns on knowledge_item_user_dismissed: {cols}"
    )

    idx_names = {
        r[0]
        for r in conn.execute(
            "SELECT index_name FROM duckdb_indexes "
            "WHERE table_name = 'knowledge_item_user_dismissed'"
        ).fetchall()
    }
    assert "idx_knowledge_item_user_dismissed_user" in idx_names, (
        f"index on user_id missing: {idx_names}"
    )
    conn.close()


def test_v45_db_migrates_cleanly_to_v46(tmp_path):
    """A pre-existing v45 DB (no dismiss table) climbs to v46 without error."""
    db_path = tmp_path / "v45.duckdb"
    conn = duckdb.connect(str(db_path))

    # Stand up a minimal v45-shape: schema_version row pinned at 45 plus a
    # knowledge_items table with one survivor row that must come through
    # the migration intact.
    conn.execute(
        "CREATE TABLE schema_version (version INTEGER, applied_at TIMESTAMP DEFAULT current_timestamp)"
    )
    conn.execute("INSERT INTO schema_version (version) VALUES (45)")
    conn.execute(
        """CREATE TABLE knowledge_items (
            id VARCHAR PRIMARY KEY,
            title VARCHAR NOT NULL,
            content TEXT,
            category VARCHAR,
            status VARCHAR DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT current_timestamp,
            updated_at TIMESTAMP
        )"""
    )
    conn.execute(
        "INSERT INTO knowledge_items (id, title, content, category, status) "
        "VALUES ('legacy', 'Legacy', 'still here', 'engineering', 'approved')"
    )

    _ensure_schema(conn)

    assert get_schema_version(conn) == SCHEMA_VERSION

    tables = {
        r[0]
        for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchall()
    }
    assert "knowledge_item_user_dismissed" in tables

    # Pre-existing knowledge_items row survived the migration.
    row = conn.execute(
        "SELECT id, title, status FROM knowledge_items WHERE id = 'legacy'"
    ).fetchone()
    assert row == ("legacy", "Legacy", "approved")

    # Inserts work, ON CONFLICT path is idempotent.
    conn.execute(
        "INSERT INTO knowledge_item_user_dismissed (user_id, item_id) VALUES ('u1', 'legacy')"
    )
    conn.execute(
        "INSERT INTO knowledge_item_user_dismissed (user_id, item_id) VALUES ('u1', 'legacy') "
        "ON CONFLICT (user_id, item_id) DO NOTHING"
    )
    cnt = conn.execute(
        "SELECT COUNT(*) FROM knowledge_item_user_dismissed WHERE user_id = 'u1' AND item_id = 'legacy'"
    ).fetchone()[0]
    assert cnt == 1, "primary key + ON CONFLICT DO NOTHING must collapse duplicate inserts"
    conn.close()


def test_v45_to_v46_function_is_idempotent(tmp_path):
    """Calling ``_v45_to_v46`` twice on the same DB is a no-op the second time."""
    db_path = tmp_path / "twice.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)
    # Re-running the migration step directly must not error — CREATE TABLE
    # IF NOT EXISTS / CREATE INDEX IF NOT EXISTS are idempotent by design.
    _v45_to_v46(conn)
    _v45_to_v46(conn)
    assert get_schema_version(conn) == SCHEMA_VERSION
    conn.close()
