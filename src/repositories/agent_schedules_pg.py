"""Postgres-backed repository for ``agent_schedules`` (v119, agent schedules).

Mirrors ``src/repositories/agent_schedules.py`` (the DuckDB impl) on the
``AgentSchedulesRepository`` public surface. Cross-engine parity is covered
by ``tests/db_pg/test_agent_schedules_contract.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine

_UPDATABLE = frozenset({"name", "schedule", "prompt", "enabled"})


class AgentSchedulesPgRepository:
    """Postgres twin of ``AgentSchedulesRepository``."""

    def __init__(self, engine: Engine):
        self._engine = engine

    def create(
        self,
        id: str,
        agent_id: str,
        name: str,
        schedule: str,
        prompt: str,
        enabled: bool = True,
    ) -> None:
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """
                    INSERT INTO agent_schedules
                      (id, agent_id, name, schedule, prompt, enabled, created_at, updated_at)
                    VALUES
                      (:id, :agent_id, :name, :schedule, :prompt, :enabled, :created_at, :updated_at)
                    """
                ),
                {
                    "id": id,
                    "agent_id": agent_id,
                    "name": name,
                    "schedule": schedule,
                    "prompt": prompt,
                    "enabled": enabled,
                    "created_at": now,
                    "updated_at": now,
                },
            )

    def get(self, id: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = conn.execute(sa.text("SELECT * FROM agent_schedules WHERE id = :id"), {"id": id}).mappings().first()
        return dict(row) if row else None

    def get_by_name(self, agent_id: str, name: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text("SELECT * FROM agent_schedules WHERE agent_id = :agent_id AND name = :name"),
                    {"agent_id": agent_id, "name": name},
                )
                .mappings()
                .first()
            )
        return dict(row) if row else None

    def list_for_agent(self, agent_id: str) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text("SELECT * FROM agent_schedules WHERE agent_id = :agent_id ORDER BY created_at"),
                    {"agent_id": agent_id},
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    def count_for_agent(self, agent_id: str) -> int:
        with self._engine.connect() as conn:
            value = conn.execute(
                sa.text("SELECT COUNT(*) FROM agent_schedules WHERE agent_id = :agent_id"),
                {"agent_id": agent_id},
            ).scalar_one()
        return int(value)

    def list_enabled(self) -> List[Dict[str, Any]]:
        """See ``AgentSchedulesRepository.list_enabled``'s docstring."""
        with self._engine.connect() as conn:
            rows = (
                conn.execute(sa.text("SELECT * FROM agent_schedules WHERE enabled = TRUE ORDER BY created_at"))
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    def update(self, id: str, **fields: Any) -> None:
        bad = set(fields) - _UPDATABLE
        if bad:
            raise ValueError(f"cannot update non-whitelisted field(s): {sorted(bad)}")
        if not fields:
            return
        set_clauses = [f"{col} = :{col}" for col in fields]
        params: Dict[str, Any] = dict(fields)
        set_clauses.append("updated_at = :updated_at")
        params["updated_at"] = datetime.now(timezone.utc)
        params["id"] = id
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(f"UPDATE agent_schedules SET {', '.join(set_clauses)} WHERE id = :id"),
                params,
            )

    def delete(self, id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(sa.text("DELETE FROM agent_schedules WHERE id = :id"), {"id": id})

    def delete_for_agent(self, agent_id: str) -> None:
        """See ``AgentSchedulesRepository.delete_for_agent``'s docstring."""
        with self._engine.begin() as conn:
            conn.execute(sa.text("DELETE FROM agent_schedules WHERE agent_id = :agent_id"), {"agent_id": agent_id})

    def claim_for_run(self, id: str, expected_last_run_at: Optional[datetime], now: datetime) -> bool:
        """See ``AgentSchedulesRepository.claim_for_run``'s docstring — same
        NULL-safe ``IS NOT DISTINCT FROM`` comparison, standard SQL on both
        engines."""
        with self._engine.begin() as conn:
            row = conn.execute(
                sa.text(
                    """
                    UPDATE agent_schedules
                    SET last_run_at = :now, updated_at = :now
                    WHERE id = :id AND last_run_at IS NOT DISTINCT FROM :expected
                    RETURNING id
                    """
                ),
                {"now": now, "id": id, "expected": expected_last_run_at},
            ).first()
        return row is not None

    def record_dispatch_result(self, id: str, status: str, job_id: Optional[str] = None) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE agent_schedules SET last_status = :status, last_job_id = :job_id, "
                    "updated_at = :updated_at WHERE id = :id"
                ),
                {"status": status, "job_id": job_id, "updated_at": datetime.now(timezone.utc), "id": id},
            )
