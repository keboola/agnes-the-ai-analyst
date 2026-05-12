"""UsageProcessor — extracts skill / agent / tool invocation events from
Claude Code session jsonls. See Phase A.3 of platform-telemetry epic."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import duckdb

from services.session_pipeline.contract import ProcessorResult
from services.session_pipeline.lib import parse_jsonl
from services.session_processors.usage_lib import (
    USAGE_PROCESSOR_VERSION,
    AttributionLookup,
    compute_summary,
    iter_events,
)
from src.repositories.usage import UsageRepository

logger = logging.getLogger(__name__)


class UsageProcessor:
    name: str = "usage"
    cadence_minutes: int = 10

    def process_session(
        self,
        session_path: Path,
        username: str,
        session_key: str,
        conn: duckdb.DuckDBPyConnection,
    ) -> ProcessorResult:
        turns = parse_jsonl(session_path)
        events = list(iter_events(turns))

        # Derive session_id from first turn that carries one
        session_id = session_key
        for t in turns:
            sid = t.get("sessionId")
            if sid:
                session_id = sid
                break

        attr = AttributionLookup(conn)
        rows = []
        for e in events:
            source, ref_id = attr.attribute(e)
            # Stable dedup key: session_id + event_uuid + event_type + tool_name
            # event_uuid may be None for slash-command turns, so we include
            # event_type and tool_name to keep the key unique within a session.
            event_uuid_part = e.event_uuid or ""
            id_input = (
                f"{session_id}|{event_uuid_part}"
                f"|{e.event_type}|{e.tool_name or ''}"
                f"|{e.command_name or ''}"
            )
            event_id = hashlib.sha256(id_input.encode()).hexdigest()
            rows.append(
                {
                    "id": event_id,
                    "session_id": session_id,
                    "session_file": session_key,
                    "username": username,
                    "event_uuid": e.event_uuid,
                    "parent_uuid": e.parent_uuid,
                    "event_type": e.event_type,
                    "tool_name": e.tool_name,
                    "skill_name": e.skill_name,
                    "subagent_type": e.subagent_type,
                    "command_name": e.command_name,
                    "is_error": e.is_error,
                    "source": source,
                    "ref_id": ref_id,
                    "model": e.model,
                    "cwd": e.cwd,
                    "occurred_at": e.occurred_at,
                    "processor_version": USAGE_PROCESSOR_VERSION,
                }
            )

        summary = compute_summary(turns, rows)
        summary["session_file"] = session_key
        summary["username"] = username
        # Override session_id with the resolved one
        if not summary.get("session_id"):
            summary["session_id"] = session_id

        repo = UsageRepository(conn)
        inserted = repo.upsert_events(rows, processor_version=USAGE_PROCESSOR_VERSION)
        repo.upsert_summary(summary, processor_version=USAGE_PROCESSOR_VERSION)

        logger.info(
            "UsageProcessor: %s — %d events extracted (%d new)",
            session_key,
            len(rows),
            inserted,
        )
        return ProcessorResult(items_count=len(rows))
