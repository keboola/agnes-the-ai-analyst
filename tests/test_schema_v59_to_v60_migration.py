"""v59 → v60: backfill ``usage_events.username`` /
``usage_session_summary.username`` from ``users.email`` where the row
has a resolved ``user_id``.

Pre-v60 the column was written by three writers with conflicting
semantics (full email from REST emitters, UUID from upload-API
sessions, OS-username from the legacy collector). The admin telemetry
dropdown surfaced one user as multiple rows as a result. v60 collapses
the historical data; the session-pipeline runner stops writing
divergent values going forward.

Asserts on the migration shape:

  * fresh install lands at v60
  * UUID rows with a known user_id get rewritten to the user's email
  * local-part rows get rewritten to the full email
  * orphan rows (user_id NULL or user deleted) are left intact
  * idempotent — re-running the migration is a no-op
  * SCHEMA_VERSION constant matches
"""

from __future__ import annotations

import duckdb

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
    assert SCHEMA_VERSION >= 60


def test_fresh_install_lands_at_or_past_v60(tmp_path):
    conn = duckdb.connect(str(tmp_path / "system.duckdb"))
    _ensure_schema(conn)
    assert get_schema_version(conn) >= 60
    assert "setup_tokens" in _tables(conn)


def test_fresh_install_setup_tokens_columns(tmp_path):
    conn = duckdb.connect(str(tmp_path / "system.duckdb"))
    _ensure_schema(conn)
    cols = _columns(conn, "setup_tokens")
    assert {"id", "user_id", "token_hash", "expires_at", "used_at", "created_at"}.issubset(cols)


def test_ensure_schema_is_idempotent(tmp_path):
    """Running _ensure_schema twice must not raise."""
    conn = duckdb.connect(str(tmp_path / "system.duckdb"))
    _ensure_schema(conn)
    _ensure_schema(conn)
    assert get_schema_version(conn) == SCHEMA_VERSION

def test_uuid_username_rewritten_to_email(tmp_path):
    """The motivating case: a session uploaded via /api/upload/sessions
    landed with ``usage_events.username = <user_id UUID>``. Migration
    rewrites it to the user's email so the telemetry dropdown collapses."""
    conn = duckdb.connect(str(tmp_path / "system.duckdb"))
    _ensure_schema(conn)
    conn.execute(
        "INSERT INTO users (id, email, updated_at) VALUES (?, ?, ?)",
        ["uuid-aaa", "alice@example.com", "2026-01-01"],
    )
    conn.execute(
        """
        INSERT INTO usage_events (
            id, session_id, session_file, username, event_type,
            is_error, source, occurred_at, processor_version, user_id
        ) VALUES (
            'evt1', 'sess1', 'uuid-aaa/sess1.jsonl',
            'uuid-aaa', 'tool_use', FALSE, 'curated',
            CURRENT_TIMESTAMP, 1, 'uuid-aaa'
        )
        """
    )
    _v59_to_v60(conn)
    assert conn.execute(
        "SELECT username FROM usage_events WHERE id = 'evt1'"
    ).fetchone()[0] == "alice@example.com"


def test_local_part_username_rewritten_to_email(tmp_path):
    """Sessions from the legacy collector landed with
    ``username = <os-username>`` (typically email local-part). Migration
    promotes them to the full email so they group with the upload path."""
    conn = duckdb.connect(str(tmp_path / "system.duckdb"))
    _ensure_schema(conn)
    conn.execute(
        "INSERT INTO users (id, email, updated_at) VALUES (?, ?, ?)",
        ["uuid-bbb", "bob@example.com", "2026-01-01"],
    )
    conn.execute(
        """
        INSERT INTO usage_session_summary (
            session_file, session_id, username, started_at, user_id,
            processor_version
        ) VALUES (
            'bob/sess1.jsonl', 'sess1', 'bob', CURRENT_TIMESTAMP,
            'uuid-bbb', 1
        )
        """
    )
    _v59_to_v60(conn)
    assert conn.execute(
        "SELECT username FROM usage_session_summary WHERE session_file = 'bob/sess1.jsonl'"
    ).fetchone()[0] == "bob@example.com"


def test_orphan_row_left_intact(tmp_path):
    """Rows whose ``user_id`` is NULL (pre-v45 backfill never reached
    them, or user deleted) must NOT be touched — the migration has no
    safe way to guess the intended email. They stay readable under
    whatever label was stored."""
    conn = duckdb.connect(str(tmp_path / "system.duckdb"))
    _ensure_schema(conn)
    conn.execute(
        """
        INSERT INTO usage_events (
            id, session_id, session_file, username, event_type,
            is_error, source, occurred_at, processor_version, user_id
        ) VALUES (
            'evt2', 'sess2', 'orphan/sess2.jsonl',
            'orphan', 'tool_use', FALSE, 'curated',
            CURRENT_TIMESTAMP, 1, NULL
        )
        """
    )
    _v59_to_v60(conn)
    assert conn.execute(
        "SELECT username FROM usage_events WHERE id = 'evt2'"
    ).fetchone()[0] == "orphan"


def test_v59_to_v60_is_idempotent(tmp_path):
    """Re-running _v59_to_v60 must not raise or double-update."""
    conn = duckdb.connect(str(tmp_path / "system.duckdb"))
    _ensure_schema(conn)
    conn.execute(
        "INSERT INTO users (id, email, updated_at) VALUES (?, ?, ?)",
        ["uuid-aaa", "alice@example.com", "2026-01-01"],
    )
    conn.execute(
        """
        INSERT INTO usage_events (
            id, session_id, session_file, username, event_type,
            is_error, source, occurred_at, processor_version, user_id
        ) VALUES (
            'evt1', 'sess1', 'uuid-aaa/sess1.jsonl',
            'uuid-aaa', 'tool_use', FALSE, 'curated',
            CURRENT_TIMESTAMP, 1, 'uuid-aaa'
        )
        """
    )
    _v59_to_v60(conn)
    _v59_to_v60(conn)  # second pass — no-op
    assert conn.execute(
        "SELECT username FROM usage_events WHERE id = 'evt1'"
    ).fetchone()[0] == "alice@example.com"
