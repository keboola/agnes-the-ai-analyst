"""Postgres-backed session_processor_state repository.

Mirrors ``src/repositories/session_processor_state.py``. PG ``TIMESTAMP
WITH TIME ZONE`` preserves UTC offsets across the round-trip, so we no
longer need the strip-tz step the DuckDB impl carries.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine


class SessionProcessorStatePgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    def is_processed(
        self,
        processor_name: str,
        session_file: str,
        file_hash: str,
    ) -> bool:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    """SELECT file_hash FROM session_processor_state
                       WHERE processor_name = :p AND session_file = :s"""
                ),
                {"p": processor_name, "s": session_file},
            ).first()
        if row is None:
            return False
        return row[0] == file_hash

    def mark_processed(
        self,
        processor_name: str,
        session_file: str,
        username: str,
        items_count: int,
        file_hash: str,
    ) -> None:
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """INSERT INTO session_processor_state
                        (processor_name, session_file, username, processed_at, items_extracted, file_hash)
                        VALUES (:p, :s, :u, :now, :ic, :h)
                        ON CONFLICT (processor_name, session_file) DO UPDATE
                        SET processed_at = EXCLUDED.processed_at,
                            items_extracted = EXCLUDED.items_extracted,
                            file_hash = EXCLUDED.file_hash,
                            username = EXCLUDED.username"""
                ),
                {
                    "p": processor_name, "s": session_file, "u": username,
                    "now": now, "ic": items_count, "h": file_hash,
                },
            )

    def scan_unprocessed_for(
        self,
        processor_name: str,
        session_dir: Path,
    ) -> list[tuple[str, Path]]:
        results: list[tuple[str, Path]] = []
        if not session_dir.exists():
            return results

        known: dict[str, Optional[datetime]] = {}
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    """SELECT session_file, processed_at FROM session_processor_state
                       WHERE processor_name = :p"""
                ),
                {"p": processor_name},
            ).all()
        for sf, pa in rows:
            known[sf] = pa

        for user_dir in session_dir.iterdir():
            if not user_dir.is_dir():
                continue
            username = user_dir.name
            for jsonl_file in sorted(user_dir.glob("*.jsonl")):
                key = f"{username}/{jsonl_file.name}"
                if key not in known:
                    results.append((username, jsonl_file))
                    continue
                processed_at = known[key]
                if processed_at is None:
                    results.append((username, jsonl_file))
                    continue
                try:
                    mtime_epoch = jsonl_file.stat().st_mtime
                except OSError:
                    results.append((username, jsonl_file))
                    continue
                # PG TIMESTAMPTZ keeps tz on the round-trip; compare against
                # a tz-aware mtime so we don't lose precision.
                mtime = datetime.fromtimestamp(mtime_epoch, tz=timezone.utc)
                if processed_at.tzinfo is None:
                    processed_at = processed_at.replace(tzinfo=timezone.utc)
                if mtime > processed_at:
                    results.append((username, jsonl_file))
        return results
