"""UsageProcessor — extracts skill / agent invocation events from Claude Code
session jsonls.

NOTE: extraction logic is intentionally not implemented yet. Storage shape
(DuckDB events table vs. append-only parquet event log), granularity
(per-invocation row vs. per-session aggregate), and signal sources
(tool_use blocks only vs. also slash-command markers in user messages) are
pending a separate brainstorm — see plan
~/.claude/plans/abundant-leaping-charm.md "Out of scope" section.

The class exists at this stage so that:
  - The session-pipeline framework can be exercised end-to-end with two
    registered processors, not one (catches single-processor assumptions).
  - The scheduler entry + admin endpoint routing are wired now and won't
    need a follow-up PR to add the second processor's plumbing.

process_session is a no-op that always reports 0 items extracted. The
runner still calls mark_processed so the same session isn't scanned again.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

from services.session_pipeline.contract import ProcessorResult


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
        # TODO: extraction logic — pending brainstorm on signal sources
        # (tool_use.name in {"Skill", "Task"}? slash-command markers?
        # subagent invocations?) and storage (events table? parquet log?
        # aggregates?). For now, return zero so the runner marks the
        # session processed and we don't re-scan it every tick.
        return ProcessorResult(items_count=0)
