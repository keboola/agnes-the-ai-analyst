"""DuckDB v68→v70 migration coverage: per-source MCP env (v69) + co-presence
columns/participants table (v70). After the main merge the co-presence schema
landed at v70, on top of main's v68→v69 MCP-env step."""
import duckdb
import pytest

from src.db import SCHEMA_VERSION, _ensure_schema, get_schema_version


def _cols(conn, table):
    return {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = ?",
            [table],
        ).fetchall()
    }


def test_fresh_install_has_v70_shape(tmp_path):
    conn = duckdb.connect(str(tmp_path / "system.duckdb"))
    _ensure_schema(conn)
    assert get_schema_version(conn) == SCHEMA_VERSION == 70
    # main's v69 MCP-env addition.
    assert "env" in _cols(conn, "mcp_sources")
    # our v70 co-presence additions.
    assert {"is_co_session", "ephemeral"} <= _cols(conn, "chat_sessions")
    assert "sender_email" in _cols(conn, "chat_messages")
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchall()
    }
    assert "chat_session_participants" in tables
    conn.close()


def test_v68_db_migrates_to_v70_with_backfill(tmp_path):
    """A pre-existing v68 DB upgrades cleanly up through v69 (MCP env) and
    v69→v70 (co-presence): mcp_sources.env is added, the co-presence columns
    default FALSE, sender_email backfills to the owner for existing user
    turns, and the participants table is created."""
    conn = duckdb.connect(str(tmp_path / "system.duckdb"))
    conn.execute("CREATE TABLE schema_version (version INTEGER, applied_at TIMESTAMP DEFAULT current_timestamp)")
    conn.execute("INSERT INTO schema_version (version) VALUES (68)")
    # mcp_sources exists from v64 in a real v68 DB; the v68→v69 step ALTERs it.
    conn.execute("""CREATE TABLE mcp_sources (
        id VARCHAR PRIMARY KEY, name VARCHAR NOT NULL UNIQUE,
        transport VARCHAR NOT NULL, command VARCHAR, args JSON, url VARCHAR,
        auth_method VARCHAR, auth_secret_env VARCHAR,
        enabled BOOLEAN NOT NULL DEFAULT true,
        created_at TIMESTAMP NOT NULL DEFAULT current_timestamp,
        updated_at TIMESTAMP NOT NULL DEFAULT current_timestamp
    )""")
    conn.execute("""CREATE TABLE chat_sessions (
        id VARCHAR PRIMARY KEY, user_email VARCHAR NOT NULL, surface VARCHAR NOT NULL,
        slack_channel_id VARCHAR, slack_thread_ts VARCHAR, title VARCHAR,
        started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        last_message_at TIMESTAMP, message_count INTEGER NOT NULL DEFAULT 0,
        archived BOOLEAN NOT NULL DEFAULT FALSE
    )""")
    conn.execute("""CREATE TABLE chat_messages (
        id VARCHAR PRIMARY KEY, session_id VARCHAR NOT NULL REFERENCES chat_sessions(id),
        role VARCHAR NOT NULL, content TEXT NOT NULL, tool_calls JSON,
        tokens_in INTEGER, tokens_out INTEGER, model VARCHAR,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.execute("INSERT INTO chat_sessions (id, user_email, surface) VALUES ('s1', 'owner@x.com', 'web')")
    conn.execute("INSERT INTO chat_messages (id, session_id, role, content) VALUES ('m1', 's1', 'user', 'hi')")
    conn.execute("INSERT INTO chat_messages (id, session_id, role, content) VALUES ('m2', 's1', 'assistant', 'hello')")

    _ensure_schema(conn)

    assert get_schema_version(conn) == SCHEMA_VERSION == 70
    # v69 MCP-env step ran.
    assert "env" in _cols(conn, "mcp_sources")
    # v70 co-presence step ran.
    assert {"is_co_session", "ephemeral"} <= _cols(conn, "chat_sessions")
    flags = conn.execute("SELECT is_co_session, ephemeral FROM chat_sessions WHERE id = 's1'").fetchone()
    assert flags == (False, False)
    # user turn backfilled to owner; assistant turn left NULL.
    user_sender = conn.execute("SELECT sender_email FROM chat_messages WHERE id = 'm1'").fetchone()[0]
    asst_sender = conn.execute("SELECT sender_email FROM chat_messages WHERE id = 'm2'").fetchone()[0]
    assert user_sender == "owner@x.com"
    assert asst_sender is None
    conn.close()


def test_dataclass_defaults_and_participant():
    from app.chat.types import ChatMessage, ChatSession, SessionParticipant
    import inspect

    sig = inspect.signature(ChatSession)
    assert sig.parameters["is_co_session"].default is False
    assert sig.parameters["ephemeral"].default is False
    assert inspect.signature(ChatMessage).parameters["sender_email"].default is None
    p = SessionParticipant(
        id="p1", session_id="s1", user_email="a@x.com", user_id="u1",
        role="owner", joined_at=None, left_at=None,
    )
    assert p.role == "owner" and p.left_at is None
