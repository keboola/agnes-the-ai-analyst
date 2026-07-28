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

    def delete_for_processors(self, processor_names: list[str]) -> int:
        """DELETE every state row whose processor_name is in *processor_names*.
        Returns the number of rows deleted. Empty input → 0 (no query).

        Mirrors the DuckDB sibling — count via ``RETURNING 1`` + ``.all()`` for
        cross-backend parity."""
        if not processor_names:
            return 0
        with self._engine.begin() as conn:
            rows = conn.execute(
                sa.text(
                    """DELETE FROM session_processor_state
                       WHERE processor_name = ANY(:names)
                       RETURNING 1"""
                ),
                {"names": list(processor_names)},
            ).all()
        return len(rows)

    def max_processed_at(self, processor_name: str) -> "datetime | None":
        """Most recent processed_at across all session rows for *processor_name*,
        or None if the processor has no state rows."""
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT MAX(processed_at) FROM session_processor_state "
                    "WHERE processor_name = :p"
                ),
                {"p": processor_name},
            ).first()
        return row[0] if row else None

    def activity_since(self, processor_name: str, since: datetime) -> dict:
        """Mirrors ``SessionProcessorStateRepository.activity_since``."""
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    """SELECT MAX(processed_at), SUM(items_extracted)
                       FROM session_processor_state
                       WHERE processor_name = :p AND processed_at >= :since"""
                ),
                {"p": processor_name, "since": since},
            ).first()
        last_processed_at = row[0] if row else None
        items_extracted = int(row[1] or 0) if row else 0
        return {"last_processed_at": last_processed_at, "items_extracted": items_extracted}

    def processed_session_files(self, processor_name: str) -> "set[str]":
        """The set of session_file values this processor has a state row for."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    "SELECT session_file FROM session_processor_state "
                    "WHERE processor_name = :p"
                ),
                {"p": processor_name},
            ).all()
        return {r[0] for r in rows}

    def get_states_for_session_files(
        self,
        processor_name: str,
        session_files: list[str],
    ) -> "dict[str, dict]":
        """For *processor_name*, return ``{session_file: {'processed_at': ...,
        'items_extracted': ...}}`` for each of *session_files* that has a state
        row. Empty input → ``{}``."""
        if not session_files:
            return {}
        stmt = sa.text(
            """SELECT session_file, processed_at, items_extracted
               FROM session_processor_state
               WHERE processor_name = :p
                 AND session_file IN :files"""
        ).bindparams(sa.bindparam("files", expanding=True))
        with self._engine.connect() as conn:
            rows = conn.execute(
                stmt,
                {"p": processor_name, "files": list(session_files)},
            ).all()
        return {
            r[0]: {"processed_at": r[1], "items_extracted": r[2]}
            for r in rows
        }

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
