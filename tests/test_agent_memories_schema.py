from src.db import SCHEMA_VERSION, _ensure_schema
from src.duckdb_conn import _open_duckdb


def test_v102_agent_memories(tmp_path):
    conn = _open_duckdb(str(tmp_path / "d.duckdb"))
    _ensure_schema(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info('agent_memories')").fetchall()}
    assert {
        "id",
        "agent_id",
        "owner_user_id",
        "content",
        "source_session_id",
        "status",
        "created_at",
        "activated_at",
        "archived_at",
    } <= cols
    # This test's job is "the v102 table exists" — the exact current ladder
    # version is tests/test_db_schema_version.py's job, so only assert we're
    # at least at v102 (using the live SCHEMA_VERSION constant, not a pin).
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] >= 102
    assert SCHEMA_VERSION >= 102
    conn.close()
