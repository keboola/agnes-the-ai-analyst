from src.db import _ensure_schema
from src.duckdb_conn import _open_duckdb


def test_v98_agent_memories(tmp_path):
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
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 98
    conn.close()
