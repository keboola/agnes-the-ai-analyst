"""Repository for usage_events and usage_session_summary tables (schema v41)."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import duckdb


class UsageRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def upsert_events(self, rows: list[dict], *, processor_version: int) -> int:
        """INSERT OR IGNORE keyed by event id. Returns number of input rows passed (not new inserts;
        DuckDB returns rowcount=-1 for INSERT OR IGNORE so we cannot cheaply count new vs duplicate).
        """
        if not rows:
            return 0
        cols = [
            "id",
            "session_id",
            "session_file",
            "username",
            "event_uuid",
            "parent_uuid",
            "event_type",
            "tool_name",
            "skill_name",
            "subagent_type",
            "command_name",
            "is_error",
            "source",
            "ref_id",
            "model",
            "cwd",
            "occurred_at",
            "processor_version",
            "user_id",
        ]
        placeholders = ",".join("?" for _ in cols)
        sql = f"INSERT OR IGNORE INTO usage_events ({','.join(cols)}) VALUES ({placeholders})"
        self.conn.executemany(
            sql, [[r.get(c) if c != "processor_version" else processor_version for c in cols] for r in rows]
        )
        return len(rows)

    def upsert_summary(self, summary: dict, *, processor_version: int) -> None:
        """INSERT OR REPLACE on session_file PK."""
        self.conn.execute(
            """
            INSERT OR REPLACE INTO usage_session_summary
                (session_file, session_id, username, started_at, ended_at,
                 active_seconds, wall_seconds, user_messages, assistant_messages,
                 tool_calls, tool_errors, skill_invocations, subagent_dispatches,
                 mcp_calls, slash_commands, distinct_tools, distinct_skills,
                 primary_model, input_tokens, output_tokens, cache_read_tokens,
                 cache_creation_tokens, processor_version, user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                summary["session_file"],
                summary.get("session_id", ""),
                summary["username"],
                summary.get("started_at"),
                summary.get("ended_at"),
                summary.get("active_seconds", 0),
                summary.get("wall_seconds", 0),
                summary.get("user_messages", 0),
                summary.get("assistant_messages", 0),
                summary.get("tool_calls", 0),
                summary.get("tool_errors", 0),
                summary.get("skill_invocations", 0),
                summary.get("subagent_dispatches", 0),
                summary.get("mcp_calls", 0),
                summary.get("slash_commands", 0),
                summary.get("distinct_tools", 0),
                summary.get("distinct_skills", 0),
                summary.get("primary_model"),
                summary.get("input_tokens", 0),
                summary.get("output_tokens", 0),
                summary.get("cache_read_tokens", 0),
                summary.get("cache_creation_tokens", 0),
                processor_version,
                summary.get("user_id"),
            ],
        )

    def purge_for_session(self, session_file: str) -> int:
        """DELETE events + summary for one session — used on reprocess."""
        r = self.conn.execute("DELETE FROM usage_events WHERE session_file = ?", [session_file])
        events_deleted = r.rowcount if r.rowcount else 0
        self.conn.execute("DELETE FROM usage_session_summary WHERE session_file = ?", [session_file])
        return events_deleted

    def emit_server_event(
        self,
        *,
        event_type: str,
        user_id: Optional[str],
        username: str = "",
        props: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Insert one synthetic usage_events row for a server-side product event.

        v49 unified-stack telemetry — Section 9.2 of the design spec. Reuses
        the existing usage_events table for ``stack.subscribe``,
        ``memory.dismiss``, ``data_package.view`` etc. so /admin/telemetry can
        slice them through the existing summary tools.

        Conventions:
          - ``event_type``   → goes in the column of the same name (e.g.
            ``stack.subscribe``); dotted namespacing is intentional so admins
            can prefix-filter.
          - ``source='server'`` distinguishes these from CC-session events.
          - ``session_id`` / ``session_file`` are server-synthetic UUIDs so
            the NOT NULL constraints stay satisfied. Telemetry consumers
            should key on ``user_id`` + ``event_type``, not session.
          - ``props`` is serialized into ``friction_tags`` (JSON column we
            piggyback for arbitrary event payload — the rename is tracked in
            spec Section 9 as a follow-up).
        """
        event_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """INSERT INTO usage_events
               (id, session_id, session_file, username, event_type,
                is_error, source, occurred_at, processor_version,
                friction_tags, user_id)
               VALUES (?, ?, ?, ?, ?, FALSE, 'server', ?, 1, ?, ?)""",
            [
                event_id,
                f"server-{event_id[:8]}",
                f"server/{event_type}.jsonl",
                username or (user_id or "anonymous"),
                event_type,
                now,
                json.dumps(props) if props else None,
                user_id,
            ],
        )
        return event_id

    def delete_older_than(self, days: int) -> int:
        """Retention prune — DELETE events older than now - days."""
        r = self.conn.execute(
            """
            DELETE FROM usage_events
            WHERE occurred_at < (CURRENT_TIMESTAMP - INTERVAL (?) DAY)
            """,
            [days],
        )
        return r.rowcount if r.rowcount else 0
