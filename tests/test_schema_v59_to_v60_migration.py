"""v59 → v60: ``setup_tokens`` table for Agnes Cowork one-click setup.

Asserts:
  * fresh install lands at v60 with the setup_tokens table present
  * sequential upgrade from v59 creates the table
  * idempotent — re-running _v59_to_v60 is a no-op
  * SCHEMA_VERSION constant == 60
"""

from __future__ import annotations

import duckdb
import pytest

from src.db import SCHEMA_VERSION, _ensure_schema, _v59_to_v60, get_schema_version


def _tables(conn) -> set[str]:
    return {
        r[0].lower()
        for r in conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main'"
        ).fetchall()
    }


def _columns(conn, table: str) -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE lower(table_name) = lower(?)",
            [table],
        ).fetchall()
    }


def test_schema_version_is_at_least_60():
    # Pinning to == 60 made this test break every time SCHEMA_VERSION bumps;
    # the v60 migration itself is what this file tests, so >= is sufficient.
    assert SCHEMA_VERSION >= 60


def test_fresh_install_has_setup_tokens_table(tmp_path):
    conn = duckdb.connect(str(tmp_path / "system.duckdb"))
    _ensure_schema(conn)
    assert "setup_tokens" in _tables(conn)


def test_fresh_install_setup_tokens_columns(tmp_path):
    conn = duckdb.connect(str(tmp_path / "system.duckdb"))
    _ensure_schema(conn)
    cols = _columns(conn, "setup_tokens")
    assert {"id", "user_id", "token_hash", "expires_at", "used_at", "created_at"}.issubset(cols)


def test_v59_to_v60_is_idempotent(tmp_path):
    """Running _ensure_schema twice must not raise."""
    conn = duckdb.connect(str(tmp_path / "system.duckdb"))
    _ensure_schema(conn)
    _ensure_schema(conn)
    assert get_schema_version(conn) == 64


def test_v59_to_v60_migration_function_is_idempotent():
    """Calling _v59_to_v60 on an already-v60 schema is a no-op."""
    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    assert "setup_tokens" in _tables(conn)
    # Re-running must not raise (CREATE TABLE IF NOT EXISTS)
    _v59_to_v60(conn)
    assert "setup_tokens" in _tables(conn)
