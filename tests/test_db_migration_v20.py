"""v20 adds source_query column to table_registry.

Backs query_mode='materialized' for BigQuery: admin registers a SQL body
that the scheduler runs through the DuckDB BQ extension and writes as a
parquet to /data/extracts/bigquery/data/<id>.parquet.

The v19 step (#150) drops dataset_permissions, access_requests tables and
users.role, table_registry.is_public columns; v20 then ALTERs the post-v19
table_registry to add the source_query column.
"""
import duckdb

from src.db import SCHEMA_VERSION, _ensure_schema, get_schema_version


def test_schema_version_is_20():
    assert SCHEMA_VERSION == 20


def test_v20_adds_source_query(tmp_path):
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)

    cols = {
        r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'table_registry'"
        ).fetchall()
    }
    assert "source_query" in cols, f"source_query missing from {cols}"
    assert get_schema_version(conn) == 20
    conn.close()


def test_v19_db_migrates_to_v20(tmp_path):
    """Pre-existing v19 DB (post-RBAC-drop) without source_query upgrades
    cleanly without losing data."""
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))

    # Simulate a v19 DB at minimal but realistic shape: schema_version row +
    # a table_registry row in the post-v19 column shape (no is_public column,
    # since v19 finalize dropped it via the table-rebuild idiom).
    conn.execute(
        "CREATE TABLE schema_version (version INTEGER, "
        "applied_at TIMESTAMP DEFAULT current_timestamp)"
    )
    conn.execute("INSERT INTO schema_version (version) VALUES (19)")
    conn.execute("""CREATE TABLE table_registry (
        id VARCHAR PRIMARY KEY, name VARCHAR NOT NULL,
        source_type VARCHAR, bucket VARCHAR, source_table VARCHAR,
        sync_strategy VARCHAR DEFAULT 'full_refresh',
        query_mode VARCHAR DEFAULT 'local',
        sync_schedule VARCHAR, profile_after_sync BOOLEAN DEFAULT true,
        primary_key VARCHAR, folder VARCHAR, description TEXT,
        registered_by VARCHAR,
        registered_at TIMESTAMP DEFAULT current_timestamp
    )""")
    conn.execute("INSERT INTO table_registry (id, name) VALUES ('foo', 'foo')")

    _ensure_schema(conn)

    assert get_schema_version(conn) == 20
    cols = {
        r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'table_registry'"
        ).fetchall()
    }
    assert "source_query" in cols
    # Existing row preserved, new column NULL
    row = conn.execute(
        "SELECT id, source_query FROM table_registry WHERE id='foo'"
    ).fetchone()
    assert row == ("foo", None)
    conn.close()
