"""v61 -> v62: ``mcp_secrets`` table added.

Mirrors the structural smoke from the existing v59 -> v60 test:

* Fresh ``_ensure_schema`` on a brand-new DB lands at v62 with the
  table present and the expected columns.
* Manually-rolled-back v61 DB (set schema_version, drop the table)
  re-migrates cleanly through the per-step ladder.
"""
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


def test_fresh_install_lands_at_v62_with_mcp_secrets(fresh_conn):
    _ensure_schema(fresh_conn)
    version = fresh_conn.execute("SELECT version FROM schema_version").fetchone()[0]
    assert version == SCHEMA_VERSION == 63

    cols = {
        r[1]: r[2]
        for r in fresh_conn.execute("PRAGMA table_info(mcp_secrets)").fetchall()
    }
    assert "source_id" in cols
    assert "secret_value_enc" in cols
    assert "created_at" in cols
    assert "updated_at" in cols


def test_existing_v61_upgrades_to_v62(fresh_conn):
    """Simulate a v61 DB by running the full migration ladder, then
    forcibly drop the v62 table and roll the version back. The next
    _ensure_schema call should re-create the table cleanly via the
    incremental ladder."""
    _ensure_schema(fresh_conn)

    fresh_conn.execute("DROP TABLE mcp_secrets")
    fresh_conn.execute("UPDATE schema_version SET version = 61")

    _ensure_schema(fresh_conn)
    version = fresh_conn.execute("SELECT version FROM schema_version").fetchone()[0]
    # Rolling back v61 → incremental ladder runs to current SCHEMA_VERSION.
    assert version == SCHEMA_VERSION
    # Table back, and idempotent
    assert fresh_conn.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'mcp_secrets'"
    ).fetchone()[0] == 1
