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
        processor needs to (re)process: no state row, OR state row with
        an mtime newer than the stored processed_at (file modified since
        last run — likely a live-append from an active Claude Code session).

        The mtime precheck is a cheap stat-only optimization: for stable
        sessions (mtime <= processed_at) we skip without reading the file.
        Files that survive the precheck still go through the runner's
        per-file ``is_processed(file_hash)`` check for authoritative
        hash-based invalidation. Without this filter, the runner would
        MD5-rehash every stable session on every scheduler tick.
        """
        results: list[tuple[str, Path]] = []
        if not session_dir.exists():
            return results

        # One query per scan, not per file. Storing processed_at (not file_hash)
        # because mtime is the cheap precheck — file_hash compare lives in the
        # runner where it's already paying the IO cost to hash.
        known: dict[str, Optional[datetime]] = {}
        rows = self.conn.execute(
            """SELECT session_file, processed_at FROM session_processor_state
                WHERE processor_name = ?""",
            [processor_name],
        ).fetchall()
        for sf, pa in rows:
            known[sf] = pa

        for user_dir in session_dir.iterdir():
            if not user_dir.is_dir():
                continue
            username = user_dir.name
            for jsonl_file in sorted(user_dir.glob("*.jsonl")):
                key = f"{username}/{jsonl_file.name}"
                if key not in known:
                    # No state row → definitely needs processing.
                    results.append((username, jsonl_file))
                    continue
                processed_at = known[key]
                if processed_at is None:
                    # Defensive: row without processed_at shouldn't happen
                    # (mark_processed always sets it), but if it does,
                    # surface for the runner.
                    results.append((username, jsonl_file))
                    continue
                try:
                    mtime_epoch = jsonl_file.stat().st_mtime
                except OSError:
                    # Stat failure: surface for the runner — it'll fail the
                    # hash compute next and report a clean error in stats
                    # rather than us silently dropping the file here.
                    results.append((username, jsonl_file))
                    continue
                # Compare in naive-UTC: the DuckDB connection helper
                # (`src.db._open_duckdb`) pins the session timezone to UTC,
                # so `processed_at` reads as UTC-clock-naive. Convert the
                # file's epoch mtime to UTC-naive on the same axis.
                mtime = datetime.fromtimestamp(mtime_epoch, tz=timezone.utc).replace(tzinfo=None)
                if processed_at.tzinfo is not None:
                    processed_at = processed_at.replace(tzinfo=None)
                if mtime > processed_at:
                    # File touched since last run — could be a live-append
                    # (Claude Code writing to an active session). Surface
                    # for the runner; its hash compare will skip if content
                    # is identical (some editors rewrite-without-change).
                    results.append((username, jsonl_file))
                # else: stable session, skip without hashing.
        return results
