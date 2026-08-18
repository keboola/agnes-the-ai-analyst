"""Repository for scheduled agent runs (v119, agent schedules).

Design doc: docs/superpowers/specs/2026-08-17-agent-schedules-design.md.
A schedule is a run-type label (``name``, unique per agent) + a cadence
string in the product's shared schedule grammar (``src.scheduler.
is_valid_schedule``) + the prompt sent to the agent on each fire.

``claim_for_run`` is the atomic dispatch primitive: the run-due sweep
(``POST /api/v1/agents/run-due``) reads a row, decides due-ness with
``src.scheduler.is_table_due``, then calls this with the ``last_run_at`` it
just read — the UPDATE only lands if that value is unchanged, so a second,
concurrent sweep tick can't double-fire the same row (optimistic
concurrency, no row lock needed).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import duckdb

_UPDATABLE = frozenset({"name", "schedule", "prompt", "enabled"})


class AgentSchedulesRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def _row_to_dict(self, row) -> Optional[Dict[str, Any]]:
        if not row:
            return None
        columns = [desc[0] for desc in self.conn.description]
        return dict(zip(columns, row))

    def _rows_to_dicts(self, rows) -> List[Dict[str, Any]]:
        if not rows:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, r)) for r in rows]

    def create(
        self,
        id: str,
        agent_id: str,
        name: str,
        schedule: str,
        prompt: str,
        enabled: bool = True,
    ) -> None:
        # last_run_at is stamped at creation so the cadence anchors here: a
        # brand-new row is NOT immediately due (is_table_due treats "never
        # run" as always-due, and an unattended agent run spends tokens — the
        # catch-up-on-create surprise a data sync tolerates is wrong for
        # agents; Devin Review on #1404). First fire = next cadence tick.
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """INSERT INTO agent_schedules
            (id, agent_id, name, schedule, prompt, enabled, last_run_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [id, agent_id, name, schedule, prompt, enabled, now, now, now],
        )

    def get(self, id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM agent_schedules WHERE id = ?", [id]).fetchone()
        return self._row_to_dict(row)

    def get_by_name(self, agent_id: str, name: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM agent_schedules WHERE agent_id = ? AND name = ?",
            [agent_id, name],
        ).fetchone()
        return self._row_to_dict(row)

    def list_for_agent(self, agent_id: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM agent_schedules WHERE agent_id = ? ORDER BY created_at",
            [agent_id],
        ).fetchall()
        return self._rows_to_dicts(rows)

    def count_for_agent(self, agent_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM agent_schedules WHERE agent_id = ?",
            [agent_id],
        ).fetchone()
        return int(row[0]) if row else 0

    def list_enabled(self) -> List[Dict[str, Any]]:
        """Every enabled row, across all agents/owners — the run-due sweep's
        walk set. Due-ness/agent-liveness is decided by the caller, one row
        at a time, so a single bad row never aborts the sweep."""
        rows = self.conn.execute(
            "SELECT * FROM agent_schedules WHERE enabled = TRUE ORDER BY created_at",
        ).fetchall()
        return self._rows_to_dicts(rows)

    def update(self, id: str, **fields: Any) -> None:
        bad = set(fields) - _UPDATABLE
        if bad:
            raise ValueError(f"cannot update non-whitelisted field(s): {sorted(bad)}")
        if not fields:
            return
        set_clauses = [f"{col} = ?" for col in fields]
        params: List[Any] = list(fields.values())
        set_clauses.append("updated_at = ?")
        params.append(datetime.now(timezone.utc))
        params.append(id)
        self.conn.execute(
            f"UPDATE agent_schedules SET {', '.join(set_clauses)} WHERE id = ?",
            params,
        )

    def delete(self, id: str) -> None:
        self.conn.execute("DELETE FROM agent_schedules WHERE id = ?", [id])

    def delete_for_agent(self, agent_id: str) -> None:
        """Cascade leg of agent delete — schedules die with the agent."""
        self.conn.execute("DELETE FROM agent_schedules WHERE agent_id = ?", [agent_id])

    def claim_for_run(self, id: str, expected_last_run_at: Optional[datetime], now: datetime) -> bool:
        """Atomically set ``last_run_at = now`` iff it still equals
        ``expected_last_run_at`` (the value the caller read just before
        deciding due-ness).

        Returns True iff this caller won the claim. ``IS NOT DISTINCT FROM``
        is NULL-safe (a never-run row reads ``last_run_at IS NULL``), and is
        standard SQL supported identically on the Postgres sibling.
        """
        result = self.conn.execute(
            """UPDATE agent_schedules
               SET last_run_at = ?, updated_at = ?
               WHERE id = ? AND last_run_at IS NOT DISTINCT FROM ?
               RETURNING id""",
            [now, now, id, expected_last_run_at],
        ).fetchone()
        return result is not None

    def record_dispatch_result(self, id: str, status: str, job_id: Optional[str] = None) -> None:
        """Record the outcome of one dispatch attempt (``'enqueued'`` or
        ``'failed_enqueue'``) — terminal job outcomes live on the job row +
        webhooks, not here."""
        self.conn.execute(
            "UPDATE agent_schedules SET last_status = ?, last_job_id = ?, updated_at = ? WHERE id = ?",
            [status, job_id, datetime.now(timezone.utc), id],
        )
