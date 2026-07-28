"""v62 -> v63: ``mcp_user_secrets`` table + ``mcp_sources.scope`` column."""
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


def test_fresh_install_lands_at_v63_with_per_user_secrets(fresh_conn):
    _ensure_schema(fresh_conn)
    version = fresh_conn.execute("SELECT version FROM schema_version").fetchone()[0]
    assert version == SCHEMA_VERSION

    cols = {r[1] for r in fresh_conn.execute("PRAGMA table_info(mcp_user_secrets)").fetchall()}
    assert {"source_id", "user_id", "secret_value_enc"}.issubset(cols)

    # mcp_sources.scope present with default 'shared'
    src_cols = {
        r[1]: r[4] for r in fresh_conn.execute("PRAGMA table_info(mcp_sources)").fetchall()
    }
    assert "scope" in src_cols


def test_existing_v62_upgrades_to_v63(fresh_conn):
    _ensure_schema(fresh_conn)

    fresh_conn.execute("DROP TABLE mcp_user_secrets")
    fresh_conn.execute("ALTER TABLE mcp_sources DROP COLUMN scope")
    fresh_conn.execute("UPDATE schema_version SET version = 62")

    _ensure_schema(fresh_conn)
    version = fresh_conn.execute("SELECT version FROM schema_version").fetchone()[0]
    # Rolling back to v62 → ladder runs all incremental steps to current.
    assert version == SCHEMA_VERSION
    assert fresh_conn.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'mcp_user_secrets'"
    ).fetchone()[0] == 1
    # scope column is back
    cols = {r[1] for r in fresh_conn.execute("PRAGMA table_info(mcp_sources)").fetchall()}
    assert "scope" in cols
