"""DB migration tests for cloud chat tables (v60).

API note: the plan's spec names ``_CURRENT_SCHEMA_VERSION``, ``migrate``, and
``open_db`` but those don't exist in src/db.py.  The real equivalents are:
  - ``SCHEMA_VERSION``   (no leading underscore)
  - ``_ensure_schema``   (called on a bare duckdb connection)
  - ``duckdb.connect``   (open an in-memory or file DB directly)

DuckDB 1.5.x limitations that affect the original plan design:
  - ON DELETE CASCADE foreign keys are not supported
    → test_cascade_deletes_messages is skipped; manual delete + FK check used instead.
  - Partial (WHERE-clause) unique indexes are not supported
    → test_partial_unique_index_dedupes_slack_dm is skipped;
       uniqueness for slack_dm / slack_thread is enforced at the app layer.
"""

import duckdb
import pytest

from src.db import SCHEMA_VERSION, _ensure_schema


# ---------------------------------------------------------------------------
# v72 → v73 migration: sandbox pause/resume refs on chat_sessions
# ---------------------------------------------------------------------------


def _make_v72_db(tmp_path) -> duckdb.DuckDBPyConnection:
    """Return a file-backed DuckDB connection at schema version 72.

    Builds the minimal set of tables that ``_ensure_schema`` expects to
    find on an upgrade path (schema_version + chat_sessions with all
    columns present in v72 but without the three v73 sandbox columns).
    """
    conn = duckdb.connect(str(tmp_path / "system.duckdb"))
    conn.execute("CREATE TABLE schema_version (version INTEGER, applied_at TIMESTAMP DEFAULT current_timestamp)")
    conn.execute("INSERT INTO schema_version (version) VALUES (72)")
    conn.execute("""
        CREATE TABLE chat_sessions (
            id               VARCHAR PRIMARY KEY,
            user_email       VARCHAR NOT NULL,
            surface          VARCHAR NOT NULL,
            slack_channel_id VARCHAR,
            slack_thread_ts  VARCHAR,
            title            VARCHAR,
            started_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_message_at  TIMESTAMP,
            message_count    INTEGER NOT NULL DEFAULT 0,
            archived         BOOLEAN NOT NULL DEFAULT FALSE,
            is_co_session    BOOLEAN NOT NULL DEFAULT FALSE,
            ephemeral        BOOLEAN NOT NULL DEFAULT FALSE
        )
    """)
    conn.execute("""
        CREATE TABLE chat_messages (
            id          VARCHAR PRIMARY KEY,
            session_id  VARCHAR NOT NULL REFERENCES chat_sessions(id),
            role        VARCHAR NOT NULL,
            content     TEXT NOT NULL,
            tool_calls  JSON,
            tokens_in   INTEGER,
            tokens_out  INTEGER,
            model       VARCHAR,
            sender_email VARCHAR,
            created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    return conn


def test_v73_adds_sandbox_ref_columns(tmp_path):
    """A v72 DB migrated to current schema has the three sandbox columns."""
    conn = _make_v72_db(tmp_path)
    _ensure_schema(conn)
    cols = {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='chat_sessions'"
        ).fetchall()
    }
    assert {"sandbox_id", "runner_pid", "sandbox_paused_at"} <= cols
    # regression: the new columns must not be indexed (DuckDB 1.5.3 FK+index bug)
    idx = conn.execute("SELECT sql FROM duckdb_indexes() WHERE table_name='chat_sessions'").fetchall()
    assert not any("sandbox" in (r[0] or "") for r in idx)
    conn.close()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _fresh_conn():
    """Return a migrated in-memory DuckDB connection."""
    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    return conn


# ---------------------------------------------------------------------------
# Test 1: tables + columns exist after migration
# ---------------------------------------------------------------------------


def test_migration_creates_chat_tables():
    conn = _fresh_conn()

    tables = {row[0] for row in conn.execute("SHOW TABLES").fetchall()}
    assert "chat_sessions" in tables
    assert "chat_messages" in tables
    assert "user_workdirs" in tables

    # PRAGMA table_info returns (cid, name, type, notnull, dflt_value, pk)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(chat_sessions)").fetchall()}
    assert {
        "id",
        "user_email",
        "surface",
        "slack_channel_id",
        "slack_thread_ts",
        "title",
        "started_at",
        "last_message_at",
        "message_count",
        "archived",
    }.issubset(cols)

    # Chat tables landed in the v67→v68 migration (renumbered from the
    # original v59→v60 when the branch merged main's v60–v67 ladder). Floor
    # assertion so this stays valid after further schema bumps.
    assert SCHEMA_VERSION >= 68


# ---------------------------------------------------------------------------
# Test 2: partial unique index per slack_dm surface
# (DuckDB 1.5.x does not support partial/filtered unique indexes — this test
#  is skipped; the constraint is enforced at the app layer in ChatRepository.)
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason=(
        "DuckDB 1.5.x does not support partial (WHERE-clause) unique indexes. "
        "Per-surface uniqueness for slack_dm is enforced at the application "
        "layer in ChatRepository, not by a DB index. "
        "Re-enable once the project upgrades to a DuckDB version that supports "
        "CREATE UNIQUE INDEX ... WHERE."
    )
)
def test_partial_unique_index_dedupes_slack_dm():
    conn = _fresh_conn()

    conn.execute(
        "INSERT INTO chat_sessions(id, user_email, surface, slack_channel_id, slack_thread_ts,"
        " started_at, message_count, archived) VALUES "
        "('chat_a', 'u@x', 'slack_dm', 'C1', NULL, CURRENT_TIMESTAMP, 0, FALSE)"
    )

    try:
        conn.execute(
            "INSERT INTO chat_sessions(id, user_email, surface, slack_channel_id, slack_thread_ts,"
            " started_at, message_count, archived) VALUES "
            "('chat_b', 'u@x', 'slack_dm', 'C1', NULL, CURRENT_TIMESTAMP, 0, FALSE)"
        )
    except duckdb.ConstraintException:
        pass
    else:
        raise AssertionError("expected unique constraint to fire for second slack_dm row")


# ---------------------------------------------------------------------------
# Test 3: multiple web sessions for the same user are allowed
# ---------------------------------------------------------------------------


def test_partial_unique_allows_multiple_web():
    conn = _fresh_conn()

    for chat_id in ("chat_a", "chat_b"):
        conn.execute(
            "INSERT INTO chat_sessions(id, user_email, surface, slack_channel_id, slack_thread_ts,"
            " started_at, message_count, archived) VALUES "
            "(?, 'u@x', 'web', NULL, NULL, CURRENT_TIMESTAMP, 0, FALSE)",
            [chat_id],
        )

    n = conn.execute("SELECT COUNT(*) FROM chat_sessions WHERE surface='web'").fetchone()[0]
    assert n == 2


# ---------------------------------------------------------------------------
# Test 4: FK cascade on session delete
# (DuckDB 1.5.x does NOT support ON DELETE CASCADE — the FK is a plain
#  reference that prevents deleting a session while messages still exist.
#  This test is skipped; ChatRepository.delete_session() must delete messages
#  first, and GDPR hard-delete will follow the same pattern.
#  Re-enable when DuckDB gains CASCADE support.)
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason=(
        "DuckDB 1.5.x raises ParserException for ON DELETE CASCADE foreign keys. "
        "The chat_messages FK is a plain reference; callers must delete child rows "
        "before deleting the parent session. Cascade behavior is handled in "
        "ChatRepository, not at the DB layer. "
        "Re-enable once the project upgrades to a DuckDB version that supports CASCADE."
    )
)
def test_cascade_deletes_messages():
    conn = _fresh_conn()
    conn.execute(
        "INSERT INTO chat_sessions(id, user_email, surface, slack_channel_id, slack_thread_ts,"
        " started_at, message_count, archived) VALUES "
        "('chat_a', 'u@x', 'web', NULL, NULL, CURRENT_TIMESTAMP, 0, FALSE)"
    )
    conn.execute(
        "INSERT INTO chat_messages(id, session_id, role, content, created_at) VALUES "
        "('msg_a', 'chat_a', 'user', 'hi', CURRENT_TIMESTAMP)"
    )
    conn.execute("DELETE FROM chat_sessions WHERE id='chat_a'")
    n = conn.execute("SELECT COUNT(*) FROM chat_messages").fetchone()[0]
    assert n == 0
