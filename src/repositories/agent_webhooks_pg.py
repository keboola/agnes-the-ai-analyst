"""Postgres-backed repository for ``agent_webhooks`` (v97, agent-api V1b).

Mirrors ``src/repositories/agent_webhooks.py`` (the DuckDB impl) on the
``AgentWebhooksRepository`` public surface. Cross-engine parity is covered
by ``tests/db_pg/test_agent_webhooks_contract.py``. The comma-membership
test on ``events`` uses the exact same portable ``LIKE`` pattern as the
DuckDB sibling — see that module's docstring.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine

_EVENT_MEMBERSHIP_SQL = "',' || events || ',' LIKE '%,' || :event || ',%'"


class AgentWebhooksPgRepository:
    """Postgres twin of ``AgentWebhooksRepository``."""

    def __init__(self, engine: Engine):
        self._engine = engine

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
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """
                    INSERT INTO agent_webhooks
                      (id, agent_id, owner_user_id, url, secret, events, created_at, updated_at)
                    VALUES
                      (:id, :agent_id, :owner_user_id, :url, :secret, :events, :created_at, :updated_at)
                    """
                ),
                {
                    "id": id,
                    "agent_id": agent_id,
                    "owner_user_id": owner_user_id,
                    "url": url,
                    "secret": secret,
                    "events": events,
                    "created_at": now,
                    "updated_at": now,
                },
            )

    def get(self, id: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = conn.execute(sa.text("SELECT * FROM agent_webhooks WHERE id = :id"), {"id": id}).mappings().first()
        return dict(row) if row else None

    def list_for_agent(self, agent_id: str) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text("SELECT * FROM agent_webhooks WHERE agent_id = :agent_id ORDER BY created_at"),
                    {"agent_id": agent_id},
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    def list_active_for_event(self, agent_id: str, event: str) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(
                        f"""
                        SELECT * FROM agent_webhooks
                        WHERE agent_id = :agent_id AND active = TRUE AND {_EVENT_MEMBERSHIP_SQL}
                        ORDER BY created_at
                        """
                    ),
                    {"agent_id": agent_id, "event": event},
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    def delete(self, id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(sa.text("DELETE FROM agent_webhooks WHERE id = :id"), {"id": id})

    def delete_for_agent(self, agent_id: str) -> None:
        """See `AgentWebhooksRepository.delete_for_agent`'s docstring."""
        with self._engine.begin() as conn:
            conn.execute(sa.text("DELETE FROM agent_webhooks WHERE agent_id = :agent_id"), {"agent_id": agent_id})

    def record_failure(self, id: str) -> int:
        """Increment ``consecutive_failures`` and return the new count.

        Returns ``0`` (sentinel) if ``id`` no longer exists — see
        ``AgentWebhooksRepository.record_failure`` for why (the webhook can
        be deleted between a delivery job's claim and this call landing).
        """
        with self._engine.begin() as conn:
            row = (
                conn.execute(
                    sa.text(
                        """
                        UPDATE agent_webhooks
                        SET consecutive_failures = consecutive_failures + 1, updated_at = :updated_at
                        WHERE id = :id
                        RETURNING consecutive_failures
                        """
                    ),
                    {"updated_at": datetime.now(timezone.utc), "id": id},
                )
                .mappings()
                .first()
            )
        return int(row["consecutive_failures"]) if row is not None else 0

    def record_success(self, id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("UPDATE agent_webhooks SET consecutive_failures = 0, updated_at = :updated_at WHERE id = :id"),
                {"updated_at": datetime.now(timezone.utc), "id": id},
            )

    def disable(self, id: str) -> None:
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE agent_webhooks SET active = FALSE, disabled_at = :disabled_at, "
                    "updated_at = :updated_at WHERE id = :id"
                ),
                {"disabled_at": now, "updated_at": now, "id": id},
            )
