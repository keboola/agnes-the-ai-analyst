"""Tests for rebuild_rollups — usage_tool_daily and usage_plugin_daily."""

from __future__ import annotations

from datetime import date, datetime, timezone, timedelta

import duckdb
import pytest

from services.session_processors.usage_lib import rebuild_rollups


def _fresh_db(tmp_path, monkeypatch) -> duckdb.DuckDBPyConnection:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import src.db as db_module
    db_module._system_db_conn = None
    db_module._system_db_path = None
    return db_module.get_system_db()


def _seed_event(conn, *, occurred_at: datetime, tool_name: str, source: str,
                ref_id: str | None = None, is_error: bool = False,
                username: str = "alice", session_id: str = "s1",
                session_file: str = "s1.jsonl",
                event_id: str | None = None):
    """Insert a minimal usage_event row."""
    import hashlib
    eid = event_id or hashlib.sha256(
        f"{session_id}|{occurred_at}|{tool_name}|{session_file}".encode()
    ).hexdigest()
    conn.execute(
        """
        INSERT OR IGNORE INTO usage_events
            (id, session_id, session_file, username, event_uuid, event_type,
             tool_name, is_error, source, ref_id, occurred_at, processor_version)
        VALUES (?, ?, ?, ?, NULL, 'tool_use', ?, ?, ?, ?, ?, 1)
        """,
        [eid, session_id, session_file, username, tool_name, is_error, source, ref_id, occurred_at],
    )


class TestRebuildRollupsToolDaily:
    def test_three_events_same_tool_same_day(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        today = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
        for i in range(3):
            _seed_event(conn, occurred_at=today, tool_name="Bash", source="builtin",
                        event_id=f"eid-bash-{i}")
        rebuild_rollups(conn, since_day=today.date())
        rows = conn.execute("SELECT * FROM usage_tool_daily").fetchall()
        assert len(rows) == 1
        desc = [d[0] for d in conn.description]
        row = dict(zip(desc, rows[0]))
        assert row["invocations"] == 3
        assert row["tool_name"] == "Bash"
        assert row["source"] == "builtin"

    def test_two_tools_same_day(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        today = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
        _seed_event(conn, occurred_at=today, tool_name="Bash", source="builtin", event_id="e1")
        _seed_event(conn, occurred_at=today, tool_name="Read", source="builtin", event_id="e2")
        rebuild_rollups(conn, since_day=today.date())
        rows = conn.execute("SELECT COUNT(*) FROM usage_tool_daily").fetchone()[0]
        assert rows == 2

    def test_error_count(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        today = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
        _seed_event(conn, occurred_at=today, tool_name="Bash", source="builtin",
                    is_error=True, event_id="e-err")
        _seed_event(conn, occurred_at=today, tool_name="Bash", source="builtin",
                    is_error=False, event_id="e-ok")
        rebuild_rollups(conn, since_day=today.date())
        row = conn.execute("SELECT error_count FROM usage_tool_daily").fetchone()
        assert row[0] == 1

    def test_distinct_users(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        today = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
        _seed_event(conn, occurred_at=today, tool_name="Bash", source="builtin",
                    username="alice", event_id="e-a")
        _seed_event(conn, occurred_at=today, tool_name="Bash", source="builtin",
                    username="bob", session_id="s2", session_file="s2.jsonl", event_id="e-b")
        rebuild_rollups(conn, since_day=today.date())
        row = conn.execute("SELECT distinct_users FROM usage_tool_daily").fetchone()
        assert row[0] == 2


class TestRebuildRollupsPluginDaily:
    def test_curated_plugin_aggregated(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        today = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
        for i in range(3):
            _seed_event(conn, occurred_at=today, tool_name="Skill", source="curated",
                        ref_id="mp/plug", event_id=f"ep-{i}")
        rebuild_rollups(conn, since_day=today.date())
        rows = conn.execute("SELECT * FROM usage_plugin_daily").fetchall()
        assert len(rows) == 1
        desc = [d[0] for d in conn.description]
        row = dict(zip(desc, rows[0]))
        assert row["invocations"] == 3
        assert row["source"] == "curated"
        assert row["ref_id"] == "mp/plug"

    def test_flea_plugin_aggregated(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        today = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
        _seed_event(conn, occurred_at=today, tool_name="Skill", source="flea",
                    ref_id="entity-1", event_id="ef-1")
        rebuild_rollups(conn, since_day=today.date())
        row = conn.execute("SELECT source, ref_id FROM usage_plugin_daily").fetchone()
        assert row is not None
        assert row[0] == "flea"
        assert row[1] == "entity-1"

    def test_builtin_not_in_plugin_daily(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        today = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
        _seed_event(conn, occurred_at=today, tool_name="Bash", source="builtin",
                    ref_id=None, event_id="eb-1")
        rebuild_rollups(conn, since_day=today.date())
        n = conn.execute("SELECT COUNT(*) FROM usage_plugin_daily").fetchone()[0]
        assert n == 0


class TestRebuildRollupsIncremental:
    def test_old_events_not_touched(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        old_day = datetime.now(timezone.utc) - timedelta(days=20)
        old_day = old_day.replace(hour=10, minute=0, second=0, microsecond=0)
        recent_day = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)

        _seed_event(conn, occurred_at=old_day, tool_name="Bash", source="builtin", event_id="old-1")
        _seed_event(conn, occurred_at=recent_day, tool_name="Read", source="builtin", event_id="new-1")

        # First full rebuild so both days appear
        rebuild_rollups(conn, since_day=old_day.date())
        all_rows_1 = conn.execute("SELECT COUNT(*) FROM usage_tool_daily").fetchone()[0]
        assert all_rows_1 == 2  # Bash on old_day + Read on recent_day

        # Now seed another recent event and do incremental rebuild (last 7 days only)
        _seed_event(conn, occurred_at=recent_day, tool_name="Read", source="builtin",
                    event_id="new-2", username="bob", session_id="s2", session_file="s2.jsonl")
        since = (datetime.now(timezone.utc) - timedelta(days=7)).date()
        rebuild_rollups(conn, since_day=since)

        # old_day row should still be there (not touched by incremental)
        old_invocations = conn.execute(
            "SELECT invocations FROM usage_tool_daily WHERE tool_name = 'Bash'"
        ).fetchone()
        assert old_invocations is not None
        assert old_invocations[0] == 1  # unchanged

        # recent_day Read row should be updated
        recent_invocations = conn.execute(
            "SELECT invocations FROM usage_tool_daily WHERE tool_name = 'Read'"
        ).fetchone()
        assert recent_invocations is not None
        assert recent_invocations[0] == 2  # 2 distinct events on recent_day
