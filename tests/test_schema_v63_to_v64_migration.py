"""v63 -> v64: ``data_package_tools`` junction added."""
from __future__ import annotations

import duckdb
import pytest

from src.db import SCHEMA_VERSION, _ensure_schema


@pytest.fixture
def fresh_conn(tmp_path):
    db = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db))
    yield conn
    conn.close()


def test_fresh_install_lands_at_v64_with_junction(fresh_conn):
    _ensure_schema(fresh_conn)
    assert fresh_conn.execute("SELECT version FROM schema_version").fetchone()[0] == SCHEMA_VERSION
    cols = {r[1] for r in fresh_conn.execute("PRAGMA table_info(data_package_tools)").fetchall()}
    assert {"package_id", "tool_id", "added_at"}.issubset(cols)


def test_existing_v63_upgrades_to_current(fresh_conn):
    _ensure_schema(fresh_conn)
    fresh_conn.execute("DROP TABLE data_package_tools")
    fresh_conn.execute("UPDATE schema_version SET version = 63")
    _ensure_schema(fresh_conn)
    assert fresh_conn.execute("SELECT version FROM schema_version").fetchone()[0] == SCHEMA_VERSION
    assert fresh_conn.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'data_package_tools'"
    ).fetchone()[0] == 1
