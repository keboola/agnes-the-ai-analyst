"""Schema v26 → v27 adds Keboola sync-strategy support columns to table_registry.

v26 (on main since v0.46.0) flipped Keboola query_mode='local' rows to
'materialized'. v27 (this file) layers per-strategy knobs on top so admins
can opt specific tables back to local + a sync_strategy. Existing
`sync_strategy` column (already in v18+) is reused as the dispatcher
field; the seven new columns are the per-strategy parameters.
"""
import duckdb
import pytest

from src.db import SCHEMA_VERSION, _V26_TO_V27_MIGRATIONS


def test_schema_version_constant_is_at_least_27():
    # v27 introduced the Keboola sync-strategy columns this file covers.
    # SCHEMA_VERSION may have advanced past 27 (e.g. /home page work landed
    # at v28); the v26 → v27 migration this file tests must still apply.
    # Pin lower bound, not an exact match.
    assert SCHEMA_VERSION >= 27


def test_migrations_add_seven_columns(tmp_path):
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(
        "CREATE TABLE schema_version (version INTEGER NOT NULL, "
        "applied_at TIMESTAMP DEFAULT current_timestamp)"
    )
    conn.execute("INSERT INTO schema_version (version) VALUES (26)")
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

    for sql in _V26_TO_V27_MIGRATIONS:
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
    conn.execute("INSERT INTO schema_version (version) VALUES (26)")
    conn.execute(
        "CREATE TABLE table_registry ("
        "id VARCHAR PRIMARY KEY, name VARCHAR NOT NULL, "
        "sync_strategy VARCHAR DEFAULT 'full_refresh')"
    )

    for sql in _V26_TO_V27_MIGRATIONS:
        conn.execute(sql)
    # Second pass must not raise (each ALTER uses IF NOT EXISTS)
    for sql in _V26_TO_V27_MIGRATIONS:
        conn.execute(sql)

    cols = {r[0] for r in conn.execute("DESCRIBE table_registry").fetchall()}
    assert "where_filters" in cols
