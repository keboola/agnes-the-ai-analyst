"""v20 → v21 migration: adds welcome_template singleton table."""

from pathlib import Path

import duckdb
import pytest

from src.db import SCHEMA_VERSION, _ensure_schema, get_schema_version


def _open(path: Path) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(path))


def test_v21_creates_welcome_template_table(tmp_path):
    db_path = tmp_path / "system.duckdb"
    conn = _open(db_path)
    # Pretend we're on v20: write a v20-shaped DB by running schema then
    # rolling the version row back.
    _ensure_schema(conn)
    conn.execute("UPDATE schema_version SET version = 20")
    conn.execute("DROP TABLE IF EXISTS welcome_template")
    conn.close()

    # Re-open: migration ladder runs.
    conn = _open(db_path)
    _ensure_schema(conn)
    assert get_schema_version(conn) == SCHEMA_VERSION
    # Singleton row must exist with NULL content (= use shipped default).
    rows = conn.execute(
        "SELECT id, content, updated_at, updated_by FROM welcome_template"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == 1  # singleton id
    assert rows[0][1] is None  # NULL = default
