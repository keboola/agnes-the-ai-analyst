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
    MarketplaceItemLookup,
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
        *,
        user_id: str | None = None,
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

        lookup = MarketplaceItemLookup(conn)
        rows = []
        for e in events:
            source, parent_plugin, _local, _type = lookup.resolve(e)
            # `usage_events.ref_id` carries the parent plugin name (curated)
            # or '' (flea standalone / builtin). Empty string normalised to
            # NULL for backwards compat with admin telemetry endpoints that
            # filter `ref_id IS NOT NULL`.
            ref_id = parent_plugin or None
            # Stable dedup key: session_id + event_uuid + tool_id + event_type + tool_name + command_name.
            # tool_id (tu_xxx) disambiguates parallel tool_use items in the same assistant turn
            # that share the same event_uuid, event_type, and tool_name.
            id_input = (
                f"{session_id}|{e.event_uuid or ''}|{e.tool_id or ''}"
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
                    "user_id": user_id,
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
        summary["user_id"] = user_id
        # Override session_id with the resolved one
        if not summary.get("session_id"):
            summary["session_id"] = session_id

        repo = UsageRepository(conn)
        n_written = repo.upsert_events(rows, processor_version=USAGE_PROCESSOR_VERSION)
        repo.upsert_summary(summary, processor_version=USAGE_PROCESSOR_VERSION)

        logger.info(
            "usage processor: %d events written for session %s",
            n_written,
            session_key,
        )
        return ProcessorResult(items_count=len(rows))
