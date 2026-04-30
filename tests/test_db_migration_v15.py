"""v15 adds source_query column to table_registry."""
import duckdb

from src.db import SCHEMA_VERSION, _ensure_schema, get_schema_version


def test_schema_version_is_15():
    assert SCHEMA_VERSION == 15


def test_v15_adds_source_query(tmp_path):
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)

    cols = {
        r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'table_registry'"
        ).fetchall()
    }
    assert "source_query" in cols, f"source_query missing from {cols}"
    assert get_schema_version(conn) == 15
    conn.close()


def test_v14_db_migrates_to_v15(tmp_path):
    """Pre-existing v14 DB without source_query upgrades cleanly without losing data."""
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    # Simulate a v14 DB with one row already in table_registry
    conn.execute("CREATE TABLE schema_version (version INTEGER, applied_at TIMESTAMP DEFAULT current_timestamp)")
    conn.execute("INSERT INTO schema_version (version) VALUES (14)")
    conn.execute("""CREATE TABLE table_registry (
        id VARCHAR PRIMARY KEY, name VARCHAR NOT NULL,
        source_type VARCHAR, bucket VARCHAR, source_table VARCHAR,
        sync_strategy VARCHAR DEFAULT 'full_refresh',
        query_mode VARCHAR DEFAULT 'local',
        sync_schedule VARCHAR, profile_after_sync BOOLEAN DEFAULT true,
        primary_key VARCHAR, folder VARCHAR, description TEXT,
        registered_by VARCHAR, is_public BOOLEAN DEFAULT true,
        registered_at TIMESTAMP DEFAULT current_timestamp
    )""")
    conn.execute("INSERT INTO table_registry (id, name) VALUES ('foo', 'foo')")

    _ensure_schema(conn)

    assert get_schema_version(conn) == 15
    cols = {
        r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'table_registry'"
        ).fetchall()
    }
    assert "source_query" in cols
    # Existing row preserved, new column NULL
    row = conn.execute("SELECT id, source_query FROM table_registry WHERE id='foo'").fetchone()
    assert row == ("foo", None)
    conn.close()
