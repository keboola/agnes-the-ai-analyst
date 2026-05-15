"""v41 → v42 migration: 7 new usage_* tables for telemetry."""

import duckdb
import pytest
from src.db import _ensure_schema as init_database, SCHEMA_VERSION


def test_schema_version_is_42():
    # Test name preserved for git-blame continuity; the version-pinned
    # tests in test_db_schema_version.py, test_home_stats.py and
    # test_schema_v46_migration.py carry the current commentary.
    assert SCHEMA_VERSION == 49


def test_v42_tables_exist_after_init(tmp_path):
    db_path = tmp_path / "test.duckdb"
    conn = duckdb.connect(str(db_path))
    init_database(conn)
    tables = {
        row[0]
        for row in conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='main'").fetchall()
    }
    # v46 dropped: usage_plugin_daily, usage_attribution_skills/_agents/_commands.
    # v46 added: usage_marketplace_item_daily, usage_marketplace_item_window.
    for tbl in [
        "usage_events",
        "usage_session_summary",
        "usage_tool_daily",
        "usage_marketplace_item_daily",
        "usage_marketplace_item_window",
    ]:
        assert tbl in tables, f"missing table {tbl}"
    for tbl in [
        "usage_plugin_daily",
        "usage_attribution_skills",
        "usage_attribution_agents",
        "usage_attribution_commands",
    ]:
        assert tbl not in tables, f"dropped table {tbl} still present"
    conn.close()


def test_v42_indices_exist(tmp_path):
    db_path = tmp_path / "test.duckdb"
    conn = duckdb.connect(str(db_path))
    init_database(conn)
    idx_names = {
        row[0]
        for row in conn.execute("SELECT index_name FROM duckdb_indexes WHERE table_name LIKE 'usage_%'").fetchall()
    }
    # v46 dropped: idx_usage_attr_*_lookup.
    # v46 added: idx_mid_lookup, idx_miw_lookup on the new marketplace tables.
    for idx in [
        "idx_usage_events_session",
        "idx_usage_events_user_time",
        "idx_usage_events_tool",
        "idx_usage_events_skill",
        "idx_usage_events_ref",
        "idx_usage_session_user",
        "idx_usage_session_started",
        "idx_mid_lookup",
        "idx_miw_lookup",
    ]:
        assert idx in idx_names, f"missing index {idx}"
    conn.close()


def test_v41_to_v42_is_idempotent(tmp_path):
    """Running init twice on same DB must not error and version stays 41."""
    db_path = tmp_path / "twice.duckdb"
    conn = duckdb.connect(str(db_path))
    init_database(conn)
    conn.close()
    conn = duckdb.connect(str(db_path))
    init_database(conn)
    v = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert v == 49
    conn.close()


def test_v41_db_upgrades_cleanly(tmp_path):
    """A v40-state DB (post-Activity-Center) must climb to v41 without error."""
    db_path = tmp_path / "v41.duckdb"
    conn = duckdb.connect(str(db_path))
    # Minimal v40 baseline shape — schema_version + audit_log with v40 columns.
    conn.execute("CREATE TABLE schema_version (version INTEGER, applied_at TIMESTAMP DEFAULT current_timestamp)")
    conn.execute("INSERT INTO schema_version (version) VALUES (41)")
    conn.execute("""CREATE TABLE audit_log (
        id VARCHAR PRIMARY KEY, timestamp TIMESTAMP DEFAULT current_timestamp,
        user_id VARCHAR, action VARCHAR, resource VARCHAR, params JSON,
        result VARCHAR, duration_ms INTEGER,
        params_before JSON, client_ip VARCHAR, client_kind VARCHAR, correlation_id VARCHAR
    )""")
    conn.close()
    conn = duckdb.connect(str(db_path))
    init_database(conn)
    v = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert v == 49
    # All 7 new v41 tables exist after the v40→v41 upgrade
    tables = {
        row[0]
        for row in conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='main'").fetchall()
    }
    # v46 replaced the v42 attribution/rollup tables — verify the post-v46 set.
    for tbl in [
        "usage_events",
        "usage_session_summary",
        "usage_tool_daily",
        "usage_marketplace_item_daily",
        "usage_marketplace_item_window",
    ]:
        assert tbl in tables, f"missing table {tbl} after upgrade"
    conn.close()


def test_v30_db_ladders_all_the_way_up(tmp_path):
    """Old v30-state DB must climb all the way to v41 without losing data."""
    db_path = tmp_path / "v30.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute("CREATE TABLE schema_version (version INTEGER, applied_at TIMESTAMP DEFAULT current_timestamp)")
    conn.execute("INSERT INTO schema_version (version) VALUES (30)")
    conn.execute("CREATE TABLE audit_log (id VARCHAR PRIMARY KEY)")
    conn.execute("INSERT INTO audit_log (id) VALUES ('vintage')")
    conn.close()

    conn = duckdb.connect(str(db_path))
    init_database(conn)
    v = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert v == 49
    cnt = conn.execute("SELECT COUNT(*) FROM audit_log WHERE id='vintage'").fetchone()[0]
    assert cnt == 1
    # New v41 table exists
    cnt2 = conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
    assert cnt2 == 0
    conn.close()
