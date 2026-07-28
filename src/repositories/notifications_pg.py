"""Postgres-backed notifications repositories.

Mirrors ``src/repositories/notifications.py`` — three repositories over
``telegram_links``, ``pending_codes``, ``script_registry``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine


class TelegramPgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    def link_user(self, user_id: str, chat_id: int) -> None:
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """INSERT INTO telegram_links (user_id, chat_id, linked_at)
                       VALUES (:u, :c, :now)
                       ON CONFLICT (user_id) DO UPDATE SET
                         chat_id = EXCLUDED.chat_id,
                         linked_at = EXCLUDED.linked_at"""
                ),
                {"u": user_id, "c": chat_id, "now": now},
            )

    def unlink_user(self, user_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM telegram_links WHERE user_id = :u"),
                {"u": user_id},
            )

    def get_link(self, user_id: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT * FROM telegram_links WHERE user_id = :u"),
                {"u": user_id},
            ).mappings().first()
        return dict(row) if row else None

    def get_all_links(self) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(sa.text("SELECT * FROM telegram_links")).mappings().all()
        return [dict(r) for r in rows]


class PendingCodePgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    def create_code(self, code: str, chat_id: int) -> None:
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO pending_codes (code, chat_id, created_at) VALUES (:c, :ci, :now)"
                ),
                {"c": code, "ci": chat_id, "now": now},
            )

    def verify_code(self, code: str) -> Optional[Dict[str, Any]]:
        with self._engine.begin() as conn:
            row = conn.execute(
                sa.text("SELECT * FROM pending_codes WHERE code = :c"),
                {"c": code},
            ).mappings().first()
            if not row:
                return None
            d = dict(row)
            conn.execute(
                sa.text("DELETE FROM pending_codes WHERE code = :c"),
                {"c": code},
            )
        return d


class ScriptPgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    def deploy(
        self,
        id: str,
        name: str,
        owner: Optional[str] = None,
        schedule: Optional[str] = None,
        source: str = "",
    ) -> None:
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """INSERT INTO script_registry
                       (id, name, owner, schedule, source, deployed_at)
                       VALUES (:id, :name, :owner, :schedule, :source, :now)
                       ON CONFLICT (id) DO UPDATE SET
                         name = EXCLUDED.name,
                         schedule = EXCLUDED.schedule,
                         source = EXCLUDED.source,
                         deployed_at = EXCLUDED.deployed_at"""
                ),
                {"id": id, "name": name, "owner": owner,
                 "schedule": schedule, "source": source, "now": now},
            )

    def undeploy(self, script_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM script_registry WHERE id = :id"),
                {"id": script_id},
            )

    def get(self, script_id: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT * FROM script_registry WHERE id = :id"),
                {"id": script_id},
            ).mappings().first()
        return dict(row) if row else None

    def list_all(self, owner: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM script_registry"
        params: Dict[str, Any] = {}
        if owner:
            sql += " WHERE owner = :owner"
            params["owner"] = owner
        sql += " ORDER BY name"
        with self._engine.connect() as conn:
            rows = conn.execute(sa.text(sql), params).mappings().all()
        return [dict(r) for r in rows]

    def claim_for_run(self, script_id: str) -> bool:
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            row = conn.execute(
                sa.text(
                    """UPDATE script_registry
                       SET last_status = 'running', last_run = :now
                       WHERE id = :id
                         AND last_status IS DISTINCT FROM 'running'
                       RETURNING id"""
                ),
                {"now": now, "id": script_id},
            ).first()
        return row is not None

    def record_run_result(self, script_id: str, status: str) -> None:
        if status not in ("success", "failure"):
            raise ValueError(
                f"record_run_result: status must be 'success' or 'failure', got {status!r}"
            )
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("UPDATE script_registry SET last_status = :s WHERE id = :id"),
                {"s": status, "id": script_id},
            )
