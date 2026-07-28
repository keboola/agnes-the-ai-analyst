"""uploaded_at anchor for the sessions browser (consistency spec Phase C)."""

from datetime import datetime, timedelta, timezone

import duckdb
import pytest

from src.db import _ensure_schema, _v104_to_v105


def _mk(tmp_path):
    conn = duckdb.connect(str(tmp_path / "c.duckdb"))
    _ensure_schema(conn)
    return conn


def test_v105_backfills_uploaded_at_from_upload_audit(tmp_path):
    conn = _mk(tmp_path)
    started = datetime(2026, 6, 1, tzinfo=timezone.utc)
    uploaded = datetime(2026, 7, 13, 13, 1, tzinfo=timezone.utc)
    conn.execute(
        "INSERT INTO usage_session_summary "
        "(session_file, session_id, username, started_at, processor_version) "
        "VALUES ('u-1/abc.jsonl', 'abc', 'ann@example.com', ?, 1), "
        "('u-1/nope.jsonl', 'nope', 'ann@example.com', ?, 1)",
        [started, started],
    )
    conn.execute(
        "INSERT INTO audit_log (id, timestamp, user_id, action, params) VALUES "
        "('e1', ?, 'u-1', 'session.upload', '{\"bytes\": 1, \"filename\": \"abc.jsonl\"}')",
        [uploaded],
    )
    _v104_to_v105(conn)
    # compare against the values as the engine stored them (DuckDB converts
    # tz-aware params to naive local storage — both columns identically)
    stored_upload = conn.execute("SELECT timestamp FROM audit_log WHERE id='e1'").fetchone()[0]
    stored_started = conn.execute(
        "SELECT started_at FROM usage_session_summary WHERE session_id='nope'"
    ).fetchone()[0]
    rows = dict(
        conn.execute("SELECT session_id, uploaded_at FROM usage_session_summary").fetchall()
    )
    assert rows["abc"] == stored_upload
    # no matching upload audit row → falls back to started_at
    assert rows["nope"] == stored_started
    conn.close()


def test_upsert_summary_stamps_uploaded_at_first_arrival_wins(tmp_path):
    from src.repositories.usage import UsageRepository

    conn = _mk(tmp_path)
    repo = UsageRepository(conn)
    base = {
        "session_file": "u-1/s.jsonl", "session_id": "s",
        "username": "ann@example.com", "user_id": "u-1",
        "started_at": datetime.now(timezone.utc),
    }
    repo.upsert_summary(dict(base), processor_version=1)
    first = conn.execute(
        "SELECT uploaded_at FROM usage_session_summary WHERE session_id='s'"
    ).fetchone()[0]
    assert first is not None
    repo.upsert_summary(dict(base), processor_version=1)  # re-process later
    second = conn.execute(
        "SELECT uploaded_at FROM usage_session_summary WHERE session_id='s'"
    ).fetchone()[0]
    assert second == first  # first arrival wins
    conn.close()


def test_sessions_where_anchor_uploaded(tmp_path):
    from src.repositories.usage import UsageRepository

    conn = _mk(tmp_path)
    repo = UsageRepository(conn)
    now = datetime.now(timezone.utc)
    old_started = now - timedelta(days=60)
    conn.execute(
        "INSERT INTO usage_session_summary "
        "(session_file, session_id, username, started_at, uploaded_at, processor_version) "
        "VALUES ('u/late.jsonl', 'late', 'a@example.com', ?, ?, 1), "
        "('u/fresh.jsonl', 'fresh', 'a@example.com', ?, ?, 1)",
        [old_started, now - timedelta(days=1), now, now],
    )
    since = now - timedelta(days=7)
    started_rows = repo.sessions_list(
        {"since": since, "anchor": "started"},
        sort_col="started_at", direction="desc", limit=10, offset=0,
    )
    uploaded_rows = repo.sessions_list(
        {"since": since, "anchor": "uploaded"},
        sort_col="uploaded_at", direction="desc", limit=10, offset=0,
    )
    assert {r["session_id"] for r in started_rows} == {"fresh"}
    assert {r["session_id"] for r in uploaded_rows} == {"late", "fresh"}
    conn.close()
