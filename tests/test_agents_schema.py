"""v96: agents / agent_scope / llm_usage / agent_scope_snapshots /
idempotency_keys tables + agent_id columns exist after _ensure_schema."""

from src.db import _ensure_schema
from src.duckdb_conn import _open_duckdb


def _cols(conn, table):
    # PRAGMA table_info columns are (cid, name, type, notnull, dflt_value, pk);
    # index 1 is the column name (see other _v*_migration tests in
    # tests/test_db_schema_version.py for the same convention).
    return {r[1] for r in conn.execute(f"PRAGMA table_info('{table}')").fetchall()}


def test_v96_tables_and_columns(tmp_path):
    conn = _open_duckdb(str(tmp_path / "d.duckdb"))
    _ensure_schema(conn)
    assert {
        "id",
        "owner_user_id",
        "name",
        "slug",
        "system_prompt",
        "model",
        "token_budget_monthly",
        "plugins_mode",
        "connections_mode",
        "tables_mode",
        "memory_mode",
        "memory_write_mode",
        "is_default",
        "created_at",
        "updated_at",
        "deleted_at",
    } <= _cols(conn, "agents")
    assert {"agent_id", "item_type", "item_id"} <= _cols(conn, "agent_scope")
    assert {
        "id",
        "agent_id",
        "user_id",
        "session_id",
        "model",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
        "created_at",
    } <= _cols(conn, "llm_usage")
    assert {"id", "session_id", "agent_id", "effective_scope", "created_at"} <= _cols(conn, "agent_scope_snapshots")
    assert {
        "key",
        "owner_user_id",
        "agent_id",
        "request_hash",
        "response_body",
        "status_code",
        "created_at",
        "expires_at",
    } <= _cols(conn, "idempotency_keys")
    assert "agent_id" in _cols(conn, "personal_access_tokens")
    assert "agent_id" in _cols(conn, "chat_sessions")
    conn.close()
