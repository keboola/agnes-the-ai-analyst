"""Schema v25 → v26 adds Keboola sync-strategy support columns to table_registry.

Existing column `sync_strategy` (already in v18+) is reused. v26 layers on the
per-strategy knobs needed by the new incremental/partitioned/where_filters paths.
"""
import duckdb
import pytest

from src.db import SCHEMA_VERSION, _V25_TO_V26_MIGRATIONS


def test_schema_version_constant_is_26():
    assert SCHEMA_VERSION == 26


def test_migrations_add_seven_columns(tmp_path):
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(
        "CREATE TABLE schema_version (version INTEGER NOT NULL, "
        "applied_at TIMESTAMP DEFAULT current_timestamp)"
    )
    conn.execute("INSERT INTO schema_version (version) VALUES (25)")
    conn.execute(
        "CREATE TABLE table_registry ("
        "id VARCHAR PRIMARY KEY, name VARCHAR NOT NULL, "
        "sync_strategy VARCHAR DEFAULT 'full_refresh', "
        "primary_key VARCHAR)"
    )
    conn.execute(
        "INSERT INTO table_registry (id, name, sync_strategy) "
        "VALUES ('in.c-crm.company', 'company', 'full_refresh')"
    )

    for sql in _V25_TO_V26_MIGRATIONS:
        conn.execute(sql)

    cols = {r[0] for r in conn.execute("DESCRIBE table_registry").fetchall()}
    expected_new = {
        "incremental_window_days",
        "max_history_days",
        "incremental_column",
        "where_filters",
        "partition_by",
        "partition_granularity",
        "initial_load_chunk_days",
    }
    assert expected_new.issubset(cols)

    # Existing rows untouched
    row = conn.execute(
        "SELECT sync_strategy, incremental_window_days, where_filters "
        "FROM table_registry WHERE id = 'in.c-crm.company'"
    ).fetchone()
    assert row[0] == "full_refresh"
    assert row[1] is None
    assert row[2] is None


def test_migrations_idempotent(tmp_path):
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(
        "CREATE TABLE schema_version (version INTEGER NOT NULL, "
        "applied_at TIMESTAMP DEFAULT current_timestamp)"
    )
    conn.execute("INSERT INTO schema_version (version) VALUES (25)")
    conn.execute(
        "CREATE TABLE table_registry ("
        "id VARCHAR PRIMARY KEY, name VARCHAR NOT NULL, "
        "sync_strategy VARCHAR DEFAULT 'full_refresh')"
    )

    for sql in _V25_TO_V26_MIGRATIONS:
        conn.execute(sql)
    # Second pass must not raise (each ALTER uses IF NOT EXISTS)
    for sql in _V25_TO_V26_MIGRATIONS:
        conn.execute(sql)

    cols = {r[0] for r in conn.execute("DESCRIBE table_registry").fetchall()}
    assert "where_filters" in cols
