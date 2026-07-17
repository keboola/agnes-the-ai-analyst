"""DuckDB-backed repository for the ``jobs`` table (durable job queue, v92).

Foundation for the wave-2B worker runtime. This task covers
enqueue/get/list + idempotency dedup only; the claim/lease lifecycle and
the worker loop that actually consumes queued jobs are later tasks in the
same wave.

Idempotency-key dedup note: the schema could in principle enforce
uniqueness with a *partial* unique index
(``... WHERE idempotency_key IS NOT NULL``) so a duplicate key is only
rejected while a matching job is still queued/running (a job that has
finished/failed/been cancelled frees its key for reuse). DuckDB does not
support partial indexes ("Not implemented Error: Creating partial indexes
is not supported currently"), so dedup is enforced here instead: before
inserting, ``enqueue()`` looks for an existing row with the same
``idempotency_key`` whose status is still ``'queued'`` or ``'running'``
and returns it unchanged if found. ``jobs_pg.py`` mirrors this same
app-level check (rather than a PG-only partial unique index) so both
backends have identical dedup semantics — the CONTRACT is the dedup
*behavior*, not the index.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import duckdb


class JobsRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn = conn

    @staticmethod
    def _decode(d: Dict[str, Any]) -> Dict[str, Any]:
        if isinstance(d.get("payload_json"), str):
            try:
                d["payload_json"] = json.loads(d["payload_json"]) if d["payload_json"] else {}
            except (TypeError, ValueError):
                d["payload_json"] = {}
        return d

    def _row_to_dict(self, row) -> Optional[Dict[str, Any]]:
        if not row:
            return None
        columns = [desc[0] for desc in self.conn.description]
        return self._decode(dict(zip(columns, row)))

    def _rows_to_dicts(self, rows) -> List[Dict[str, Any]]:
        if not rows:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [self._decode(dict(zip(columns, r))) for r in rows]

    def enqueue(
        self,
        kind: str,
        payload: dict,
        *,
        priority: int = 0,
        run_after: Optional[datetime] = None,
        max_attempts: int = 3,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Insert a queued job and return its row.

        If ``idempotency_key`` matches an existing job whose status is
        still ``'queued'`` or ``'running'``, that row is returned
        unchanged — no new insert (dedup). See the module docstring for
        why this check lives here rather than in a DB constraint.
        """
        if idempotency_key is not None:
            existing = self.conn.execute(
                """SELECT * FROM jobs
                   WHERE idempotency_key = ? AND status IN ('queued', 'running')
                   ORDER BY created_at LIMIT 1""",
                [idempotency_key],
            ).fetchone()
            existing_row = self._row_to_dict(existing)
            if existing_row is not None:
                return existing_row

        job_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """INSERT INTO jobs
               (id, kind, payload_json, status, priority, run_after,
                attempts, max_attempts, idempotency_key, created_at)
               VALUES (?, ?, ?, 'queued', ?, ?, 0, ?, ?, ?)""",
            [
                job_id,
                kind,
                json.dumps(payload or {}),
                priority,
                run_after,
                max_attempts,
                idempotency_key,
                now,
            ],
        )
        row = self.get(job_id)
        assert row is not None  # just inserted under our own transaction
        return row

    def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", [job_id]).fetchone()
        return self._row_to_dict(row)

    def list(
        self,
        *,
        status: Optional[str] = None,
        kind: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM jobs WHERE 1=1"
        params: List[Any] = []
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        if kind is not None:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        return self._rows_to_dicts(rows)
