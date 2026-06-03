"""Postgres-backed usage repository.

Mirrors ``src/repositories/usage.py``. ``INSERT OR IGNORE`` becomes
``ON CONFLICT DO NOTHING``; ``INSERT OR REPLACE`` becomes
``ON CONFLICT (...) DO UPDATE SET ...``.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine


_EVENT_COLS = [
    "id", "session_id", "session_file", "username",
    "event_uuid", "parent_uuid", "event_type",
    "tool_name", "skill_name", "subagent_type", "command_name",
    "is_error", "source", "ref_id", "model", "cwd",
    "occurred_at", "processor_version", "user_id",
]


class UsagePgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    def upsert_events(self, rows: list[dict], *, processor_version: int) -> int:
        if not rows:
            return 0
        placeholders = ",".join(f":{c}" for c in _EVENT_COLS)
        sql = (
            f"INSERT INTO usage_events ({','.join(_EVENT_COLS)}) "
            f"VALUES ({placeholders}) "
            f"ON CONFLICT (id) DO NOTHING"
        )
        with self._engine.begin() as conn:
            for r in rows:
                params = {c: r.get(c) for c in _EVENT_COLS}
                # Backfill the DEFAULT-FALSE columns DuckDB would coerce from
                # NULL silently. PG is strict on NOT NULL even when a default
                # exists — see GH-XXX for the equivalent INSERT semantics gap.
                if params.get("is_error") is None:
                    params["is_error"] = False
                params["processor_version"] = processor_version
                conn.execute(sa.text(sql), params)
        return len(rows)

    def emit_server_event(
        self,
        *,
        event_type: str,
        user_id: Optional[str],
        username: str = "",
        props: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Insert one synthetic usage_events row for a server-side product event.

        Mirrors ``UsageRepository.emit_server_event`` (DuckDB). ``props`` is
        serialized into the ``friction_tags`` JSONB column via
        ``CAST(:friction_tags AS JSONB)`` (the project's PG-JSONB write
        convention); ``session_id`` / ``session_file`` are server-synthetic so
        the NOT NULL constraints stay satisfied.
        """
        event_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """INSERT INTO usage_events
                       (id, session_id, session_file, username, event_type,
                        is_error, source, occurred_at, processor_version,
                        friction_tags, user_id)
                       VALUES (:id, :session_id, :session_file, :username, :event_type,
                               FALSE, 'server', :occurred_at, 1,
                               CAST(:friction_tags AS JSONB), :user_id)"""
                ),
                {
                    "id": event_id,
                    "session_id": f"server-{event_id[:8]}",
                    "session_file": f"server/{event_type}.jsonl",
                    "username": username or (user_id or "anonymous"),
                    "event_type": event_type,
                    "occurred_at": now,
                    "friction_tags": json.dumps(props) if props else None,
                    "user_id": user_id,
                },
            )
        return event_id

    def upsert_summary(self, summary: dict, *, processor_version: int) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """
                    INSERT INTO usage_session_summary
                        (session_file, session_id, username, started_at, ended_at,
                         active_seconds, wall_seconds, user_messages, assistant_messages,
                         tool_calls, tool_errors, skill_invocations, subagent_dispatches,
                         mcp_calls, slash_commands, distinct_tools, distinct_skills,
                         primary_model, input_tokens, output_tokens, cache_read_tokens,
                         cache_creation_tokens, processor_version, user_id)
                    VALUES (:sf, :sid, :u, :sa, :ea,
                            :acts, :walls, :um, :am,
                            :tc, :te, :si, :sd,
                            :mc, :sc, :dt, :ds,
                            :pm, :it, :ot, :crt,
                            :cct, :pv, :uid)
                    ON CONFLICT (session_file) DO UPDATE SET
                      session_id           = EXCLUDED.session_id,
                      username             = EXCLUDED.username,
                      started_at           = EXCLUDED.started_at,
                      ended_at             = EXCLUDED.ended_at,
                      active_seconds       = EXCLUDED.active_seconds,
                      wall_seconds         = EXCLUDED.wall_seconds,
                      user_messages        = EXCLUDED.user_messages,
                      assistant_messages   = EXCLUDED.assistant_messages,
                      tool_calls           = EXCLUDED.tool_calls,
                      tool_errors          = EXCLUDED.tool_errors,
                      skill_invocations    = EXCLUDED.skill_invocations,
                      subagent_dispatches  = EXCLUDED.subagent_dispatches,
                      mcp_calls            = EXCLUDED.mcp_calls,
                      slash_commands       = EXCLUDED.slash_commands,
                      distinct_tools       = EXCLUDED.distinct_tools,
                      distinct_skills      = EXCLUDED.distinct_skills,
                      primary_model        = EXCLUDED.primary_model,
                      input_tokens         = EXCLUDED.input_tokens,
                      output_tokens        = EXCLUDED.output_tokens,
                      cache_read_tokens    = EXCLUDED.cache_read_tokens,
                      cache_creation_tokens= EXCLUDED.cache_creation_tokens,
                      processor_version    = EXCLUDED.processor_version,
                      user_id              = EXCLUDED.user_id
                    """
                ),
                {
                    "sf": summary["session_file"],
                    "sid": summary.get("session_id", ""),
                    "u": summary["username"],
                    "sa": summary.get("started_at"),
                    "ea": summary.get("ended_at"),
                    "acts": summary.get("active_seconds", 0),
                    "walls": summary.get("wall_seconds", 0),
                    "um": summary.get("user_messages", 0),
                    "am": summary.get("assistant_messages", 0),
                    "tc": summary.get("tool_calls", 0),
                    "te": summary.get("tool_errors", 0),
                    "si": summary.get("skill_invocations", 0),
                    "sd": summary.get("subagent_dispatches", 0),
                    "mc": summary.get("mcp_calls", 0),
                    "sc": summary.get("slash_commands", 0),
                    "dt": summary.get("distinct_tools", 0),
                    "ds": summary.get("distinct_skills", 0),
                    "pm": summary.get("primary_model"),
                    "it": summary.get("input_tokens", 0),
                    "ot": summary.get("output_tokens", 0),
                    "crt": summary.get("cache_read_tokens", 0),
                    "cct": summary.get("cache_creation_tokens", 0),
                    "pv": processor_version,
                    "uid": summary.get("user_id"),
                },
            )

    def purge_for_session(self, session_file: str) -> int:
        with self._engine.begin() as conn:
            rows = conn.execute(
                sa.text(
                    "DELETE FROM usage_events WHERE session_file = :sf RETURNING 1"
                ),
                {"sf": session_file},
            ).all()
            events_deleted = len(rows)
            conn.execute(
                sa.text(
                    "DELETE FROM usage_session_summary WHERE session_file = :sf"
                ),
                {"sf": session_file},
            )
        return events_deleted

    def delete_older_than(self, days: int) -> int:
        with self._engine.begin() as conn:
            rows = conn.execute(
                sa.text(
                    "DELETE FROM usage_events "
                    "WHERE occurred_at < (CURRENT_TIMESTAMP - (:days::TEXT || ' days')::INTERVAL) "
                    "RETURNING 1"
                ),
                {"days": days},
            ).all()
        return len(rows)
