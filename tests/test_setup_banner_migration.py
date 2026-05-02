"""v21 → v22 migration: adds setup_banner singleton table."""

from pathlib import Path

import duckdb

from src.db import SCHEMA_VERSION, _ensure_schema, get_schema_version


def _open(path: Path) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(path))


def test_v22_creates_setup_banner_table(tmp_path):
    db_path = tmp_path / "system.duckdb"
    conn = _open(db_path)
    # Pretend we're on v21: run schema then roll version back.
    _ensure_schema(conn)
    conn.execute("UPDATE schema_version SET version = 21")
    conn.execute("DROP TABLE IF EXISTS setup_banner")
    conn.close()

    # Re-open: migration ladder runs.
    conn = _open(db_path)
    _ensure_schema(conn)
    assert get_schema_version(conn) == SCHEMA_VERSION
    # Singleton row must exist with NULL content (= no banner).
    rows = conn.execute(
        "SELECT id, content, updated_at, updated_by FROM setup_banner"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == 1  # singleton id
    assert rows[0][1] is None  # NULL = no banner


def test_fresh_install_seeds_setup_banner(tmp_path):
    db_path = tmp_path / "system.duckdb"
    conn = _open(db_path)
    _ensure_schema(conn)
    assert get_schema_version(conn) == SCHEMA_VERSION
    rows = conn.execute("SELECT id, content FROM setup_banner").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == 1
    assert rows[0][1] is None


def test_welcome_template_unaffected_by_v22(tmp_path):
    """welcome_template table must still coexist after v22 migration."""
    db_path = tmp_path / "system.duckdb"
    conn = _open(db_path)
    _ensure_schema(conn)
    rows = conn.execute("SELECT id, content FROM welcome_template").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == 1
