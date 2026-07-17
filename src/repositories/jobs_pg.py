"""Postgres-backed repository for the ``jobs`` table (durable job queue, v92).

Mirrors ``src/repositories/jobs.py``. Idempotency dedup is enforced with
a partial unique index (``idx_jobs_idem`` — ``WHERE idempotency_key IS
NOT NULL AND status IN ('queued', 'running')``, see
``migrations/versions/0039_jobs_v92.py`` and ``src/models/jobs.py``) as
the ``ON CONFLICT`` arbiter for the insert below.

This is deliberately NOT a plain SELECT-then-INSERT: under READ
COMMITTED, two concurrent transactions can both miss each other's
uncommitted row and both insert (empirically: 8 concurrent enqueues of
the same key produced 8 rows). ``INSERT ... ON CONFLICT ... DO NOTHING``
lets Postgres's own unique-index conflict check make the race atomic —
a second transaction inserting the same still-queued/running key blocks
on the first's row lock, then sees the conflict once it commits and
takes the ``DO NOTHING`` branch instead of inserting a duplicate.

DuckDB has no partial-index support, so its sibling (``src/db.py`` /
``JobsRepository.enqueue()``) keeps the app-level check-then-insert
(guarded by an in-process lock, safe under DuckDB's single-writer
model) — see that module's docstring. The CONTRACT shared by both
backends is the dedup *behavior* (matching key + queued/running status
returns the existing row, no insert), not the mechanism.
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
        unchanged — no new insert (dedup), race-safe under concurrent
        callers. Mirrors ``JobsRepository.enqueue`` (dedup *behavior*,
        not the underlying mechanism — see the module docstring).
        """
        job_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            row = (
                conn.execute(
                    sa.text(
                        """INSERT INTO jobs
                           (id, kind, payload_json, status, priority, run_after,
                            attempts, max_attempts, idempotency_key, created_at)
                           VALUES (:id, :kind, :payload_json, 'queued', :priority, :run_after,
                                   0, :max_attempts, :idempotency_key, :created_at)
                           ON CONFLICT (idempotency_key)
                               WHERE idempotency_key IS NOT NULL AND status IN ('queued', 'running')
                           DO NOTHING
                           RETURNING *"""
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
                .mappings()
                .first()
            )
            if row is None:
                # Lost the race: another transaction holds a queued/running
                # row for this key (NULL keys never conflict — the partial
                # index excludes them — so this only happens when
                # idempotency_key is not None). Return the winner's row.
                assert idempotency_key is not None
                row = (
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
        assert row is not None  # either just inserted, or the conflicting row exists
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
