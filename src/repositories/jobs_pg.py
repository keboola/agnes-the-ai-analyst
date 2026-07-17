"""Postgres-backed repository for the ``jobs`` table (durable job queue, v92).

Mirrors ``src/repositories/jobs.py``. Idempotency dedup uses the same
app-level check-then-insert (inside one transaction) as the DuckDB side,
rather than a PG-only partial unique index, so both backends have
identical dedup semantics — see the DuckDB module's docstring for the
full rationale (DuckDB has no partial-index support, so the CONTRACT is
the dedup *behavior*, not the index).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine


class JobsPgRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    @staticmethod
    def _decode(d: Dict[str, Any]) -> Dict[str, Any]:
        if isinstance(d.get("payload_json"), str):
            try:
                d["payload_json"] = json.loads(d["payload_json"]) if d["payload_json"] else {}
            except (TypeError, ValueError):
                d["payload_json"] = {}
        return d

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
        unchanged — no new insert (dedup). Mirrors
        ``JobsRepository.enqueue``.
        """
        job_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            if idempotency_key is not None:
                existing = (
                    conn.execute(
                        sa.text(
                            """SELECT * FROM jobs
                               WHERE idempotency_key = :key AND status IN ('queued', 'running')
                               ORDER BY created_at LIMIT 1"""
                        ),
                        {"key": idempotency_key},
                    )
                    .mappings()
                    .first()
                )
                if existing is not None:
                    return self._decode(dict(existing))

            conn.execute(
                sa.text(
                    """INSERT INTO jobs
                       (id, kind, payload_json, status, priority, run_after,
                        attempts, max_attempts, idempotency_key, created_at)
                       VALUES (:id, :kind, :payload_json, 'queued', :priority, :run_after,
                               0, :max_attempts, :idempotency_key, :created_at)"""
                ),
                {
                    "id": job_id,
                    "kind": kind,
                    "payload_json": json.dumps(payload or {}),
                    "priority": priority,
                    "run_after": run_after,
                    "max_attempts": max_attempts,
                    "idempotency_key": idempotency_key,
                    "created_at": now,
                },
            )
            row = conn.execute(sa.text("SELECT * FROM jobs WHERE id = :id"), {"id": job_id}).mappings().first()
        assert row is not None  # just inserted in the same transaction
        return self._decode(dict(row))

    def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = conn.execute(sa.text("SELECT * FROM jobs WHERE id = :id"), {"id": job_id}).mappings().first()
        return self._decode(dict(row)) if row else None

    def list(
        self,
        *,
        status: Optional[str] = None,
        kind: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM jobs WHERE 1=1"
        params: Dict[str, Any] = {"limit": limit}
        if status is not None:
            sql += " AND status = :status"
            params["status"] = status
        if kind is not None:
            sql += " AND kind = :kind"
            params["kind"] = kind
        sql += " ORDER BY created_at DESC LIMIT :limit"
        with self._engine.connect() as conn:
            rows = conn.execute(sa.text(sql), params).mappings().all()
        return [self._decode(dict(r)) for r in rows]
