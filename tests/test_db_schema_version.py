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


def test_schema_version_is_28():
    # v26 → v27: Keboola sync-strategy support columns on table_registry
    # (incremental_window_days, max_history_days, incremental_column,
    # where_filters, partition_by, partition_granularity,
    # initial_load_chunk_days). Existing sync_strategy column reused;
    # admins can opt specific tables back to query_mode='local' for the
    # new dispatcher (incremental, partitioned, full_refresh + where_filters).
    # v27 → v28 (this PR): explicit-install (Model B) for curated
    # marketplace plugins. user_plugin_optouts row presence flips meaning
    # from "excluded" to "subscribed"; the migration wipes existing rows
    # so the inverted reading starts from a clean baseline. Also adds
    # marketplace_plugins.created_at (per-plugin "newest first" sort on
    # /marketplace), backfilled from parent marketplace_registry.registered_at.
    assert SCHEMA_VERSION == 28


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
    assert get_schema_version(conn) == SCHEMA_VERSION
    conn.close()


def test_v23_adds_claude_md_template(tmp_path):
    """v23 must create the claude_md_template singleton table."""
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)

    tables = {
        r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main'"
        ).fetchall()
    }
    assert "claude_md_template" in tables, f"claude_md_template missing from {tables}"

    # Singleton row seeded
    row = conn.execute("SELECT id, content FROM claude_md_template WHERE id = 1").fetchone()
    assert row is not None
    assert row[0] == 1
    assert row[1] is None  # default = no override
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

    assert get_schema_version(conn) == SCHEMA_VERSION  # bumped 19→26 forward
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
