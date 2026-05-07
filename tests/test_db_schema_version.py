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


def test_schema_version_is_26():
    # bumped 25→26 for the /home page rollout: instance_templates singleton
    # consolidation (welcome_template + claude_md_template merged) + new
    # users.onboarded column. See tests/test_v26_migration.py for the
    # exhaustive coverage; this assertion guards against accidental
    # regression of the bumped constant.
    assert SCHEMA_VERSION == 26


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


def test_claude_md_template_seeded_in_instance_templates(tmp_path):
    """v23 introduced claude_md_template as a singleton table; v26 consolidates
    it into instance_templates keyed 'claude_md'. Post-v26 the legacy table is
    dropped — the canonical lookup is `instance_templates WHERE key='claude_md'`.

    See tests/test_v26_migration.py for the migration path coverage. This test
    just verifies the seeded row is present on a fresh install.
    """
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)

    tables = {
        r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main'"
        ).fetchall()
    }
    assert "instance_templates" in tables
    assert "claude_md_template" not in tables, (
        "claude_md_template should be consolidated away post-v26"
    )

    row = conn.execute(
        "SELECT key, content FROM instance_templates WHERE key = 'claude_md'"
    ).fetchone()
    assert row is not None
    assert row[0] == "claude_md"
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

    assert get_schema_version(conn) == SCHEMA_VERSION  # bumped 19→25 forward
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
