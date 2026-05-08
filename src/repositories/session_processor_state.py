"""Repository for session_processor_state — per-(processor, session) bookkeeping
for the session pipeline framework (services/session_pipeline/).

Composite PK (processor_name, session_file) lets each processor track its own
processed-set independently. file_hash invalidates the row when a session jsonl
grows (Claude Code appending live to an active session) so processors reprocess
the new content rather than treating the first hash as final.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import duckdb


class SessionProcessorStateRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def is_processed(
        self,
        processor_name: str,
        session_file: str,
        file_hash: str,
    ) -> bool:
        """True iff a state row exists for (processor_name, session_file) AND
        the stored file_hash matches the supplied current hash. Hash mismatch
        (e.g. session jsonl grew since last run) is treated as unprocessed
        so the processor reprocesses on the next tick."""
        result = self.conn.execute(
            """SELECT file_hash FROM session_processor_state
                WHERE processor_name = ? AND session_file = ?""",
            [processor_name, session_file],
        ).fetchone()
        if result is None:
            return False
        return result[0] == file_hash

    def mark_processed(
        self,
        processor_name: str,
        session_file: str,
        username: str,
        items_count: int,
        file_hash: str,
    ) -> None:
        """UPSERT — overwrites previous state row for (processor, session)."""
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """INSERT INTO session_processor_state
                (processor_name, session_file, username, processed_at, items_extracted, file_hash)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (processor_name, session_file) DO UPDATE
                SET processed_at = excluded.processed_at,
                    items_extracted = excluded.items_extracted,
                    file_hash = excluded.file_hash,
                    username = excluded.username""",
            [processor_name, session_file, username, now, items_count, file_hash],
        )

    def scan_unprocessed_for(
        self,
        processor_name: str,
        session_dir: Path,
    ) -> list[tuple[str, Path]]:
        """Return (username, jsonl_path) pairs in *session_dir* that this
        processor has not yet processed (no state row, OR stale file_hash).

        Hash is intentionally NOT computed here — the runner computes it once
        per file and uses it both for the unprocessed check and for the
        eventual mark_processed call. Computing it twice would double the
        per-file IO cost on large session dirs.
        """
        results: list[tuple[str, Path]] = []
        if not session_dir.exists():
            return results

        # Collect all known (session_file → file_hash) pairs for this
        # processor in one query so the hash-mismatch check stays O(1) per
        # candidate file instead of per-file SELECT round-trips.
        known: dict[str, Optional[str]] = {}
        rows = self.conn.execute(
            """SELECT session_file, file_hash FROM session_processor_state
                WHERE processor_name = ?""",
            [processor_name],
        ).fetchall()
        for sf, fh in rows:
            known[sf] = fh

        for user_dir in session_dir.iterdir():
            if not user_dir.is_dir():
                continue
            username = user_dir.name
            for jsonl_file in sorted(user_dir.glob("*.jsonl")):
                key = f"{username}/{jsonl_file.name}"
                # The runner will recompute the hash + compare; here we
                # short-circuit on the cheap "no row exists" case and let
                # the runner handle hash invalidation. Returning everything
                # not in `known` keeps this method's contract simple and
                # the hash-check authoritative in one place (the runner /
                # is_processed call).
                if key not in known:
                    results.append((username, jsonl_file))
                else:
                    # Row exists; runner will recompute hash and re-check
                    # via is_processed(). Pass it through so it gets that
                    # chance — cost is one hash compute per stable session,
                    # which is in line with today's behavior anyway.
                    results.append((username, jsonl_file))
        return results
