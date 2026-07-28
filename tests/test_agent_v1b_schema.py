"""v101: agent_webhooks + agent_artifacts tables exist after _ensure_schema."""

from src.db import SCHEMA_VERSION, _ensure_schema
from src.duckdb_conn import _open_duckdb


def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info('{table}')").fetchall()}


def test_v101_tables(tmp_path):
    conn = _open_duckdb(str(tmp_path / "d.duckdb"))
    _ensure_schema(conn)
    assert {
        "id",
        "agent_id",
        "owner_user_id",
        "url",
        "secret",
        "events",
        "active",
        "consecutive_failures",
        "disabled_at",
        "created_at",
        "updated_at",
    } <= _cols(conn, "agent_webhooks")
    assert {
        "id",
        "session_id",
        "agent_id",
        "owner_user_id",
        "filename",
        "object_key",
        "size_bytes",
        "content_type",
        "md5",
        "created_at",
    } <= _cols(conn, "agent_artifacts")
    # This test's job is "the v101 tables exist" — the exact current ladder
    # version is tests/test_db_schema_version.py's job, so only assert we're
    # at least at v101 (using the live SCHEMA_VERSION constant, not a pin).
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] >= 101
    assert SCHEMA_VERSION >= 101
    conn.close()
