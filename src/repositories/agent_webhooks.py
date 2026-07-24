"""Repository for outbound agent webhook registrations (v97, agent-api V1b).

A webhook fires an HMAC-signed POST for each subscribed event in its
comma-joined ``events`` column (e.g. ``job.completed,job.failed``).
``list_active_for_event`` is the hot path the dispatcher (a later task)
polls before firing a delivery — it must return the identical result set
on both backends for a given ``(agent_id, event)`` pair. Membership on the
comma-joined column is done here with a portable ``LIKE`` pattern
(``,event,`` bracketed by sentinel commas around the stored value) rather
than a DuckDB-only or Postgres-only array/regex function, so the SQL text
is byte-identical across ``agent_webhooks.py`` and ``agent_webhooks_pg.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import duckdb

_EVENT_MEMBERSHIP_SQL = "',' || events || ',' LIKE '%,' || ? || ',%'"


class AgentWebhooksRepository:
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
        owner_user_id: str,
        url: str,
        secret: str,
        events: str,
    ) -> None:
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """INSERT INTO agent_webhooks
            (id, agent_id, owner_user_id, url, secret, events, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [id, agent_id, owner_user_id, url, secret, events, now, now],
        )

    def get(self, id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM agent_webhooks WHERE id = ?", [id]).fetchone()
        return self._row_to_dict(row)

    def list_for_agent(self, agent_id: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM agent_webhooks WHERE agent_id = ? ORDER BY created_at",
            [agent_id],
        ).fetchall()
        return self._rows_to_dicts(rows)

    def list_active_for_event(self, agent_id: str, event: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            f"""SELECT * FROM agent_webhooks
            WHERE agent_id = ? AND active = TRUE AND {_EVENT_MEMBERSHIP_SQL}
            ORDER BY created_at""",
            [agent_id, event],
        ).fetchall()
        return self._rows_to_dicts(rows)

    def delete(self, id: str) -> None:
        self.conn.execute("DELETE FROM agent_webhooks WHERE id = ?", [id])

    def record_failure(self, id: str) -> int:
        """Increment ``consecutive_failures`` and return the new count.

        Returns ``0`` (sentinel) if ``id`` no longer exists — the webhook
        can be deleted by its owner between a delivery job's claim and
        this call landing, and the UPDATE then matches zero rows. Callers
        (``app.chat.webhook_delivery.deliver``) treat ``0`` as "webhook
        vanished, stop" rather than crashing into a retry loop.
        """
        row = self.conn.execute(
            """UPDATE agent_webhooks
            SET consecutive_failures = consecutive_failures + 1, updated_at = ?
            WHERE id = ?
            RETURNING consecutive_failures""",
            [datetime.now(timezone.utc), id],
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def record_success(self, id: str) -> None:
        self.conn.execute(
            "UPDATE agent_webhooks SET consecutive_failures = 0, updated_at = ? WHERE id = ?",
            [datetime.now(timezone.utc), id],
        )

    def disable(self, id: str) -> None:
        now = datetime.now(timezone.utc)
        self.conn.execute(
            "UPDATE agent_webhooks SET active = FALSE, disabled_at = ?, updated_at = ? WHERE id = ?",
            [now, now, id],
        )
