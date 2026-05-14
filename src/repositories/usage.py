"""Repository for usage_events and usage_session_summary tables (schema v41)."""

from __future__ import annotations

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
            "id", "session_id", "session_file", "username", "event_uuid", "parent_uuid",
            "event_type", "tool_name", "skill_name", "subagent_type", "command_name",
            "is_error", "source", "ref_id", "model", "cwd", "occurred_at", "processor_version",
        ]
        placeholders = ",".join("?" for _ in cols)
        sql = f"INSERT OR IGNORE INTO usage_events ({','.join(cols)}) VALUES ({placeholders})"
        self.conn.executemany(sql, [
            [r.get(c) if c != "processor_version" else processor_version for c in cols] for r in rows
        ])
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
                 cache_creation_tokens, processor_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            ],
        )

    def purge_for_session(self, session_file: str) -> int:
        """DELETE events + summary for one session — used on reprocess."""
        r = self.conn.execute(
            "DELETE FROM usage_events WHERE session_file = ?", [session_file]
        )
        events_deleted = r.rowcount if r.rowcount else 0
        self.conn.execute(
            "DELETE FROM usage_session_summary WHERE session_file = ?", [session_file]
        )
        return events_deleted

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
