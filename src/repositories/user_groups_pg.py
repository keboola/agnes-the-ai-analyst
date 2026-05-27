"""Postgres-backed user-groups repository.

Mirrors ``src/repositories/user_groups.py``. The ``fanout_system_for_group``
call from ``create()`` is intentionally a soft-fail when the
``marketplace_plugins`` table isn't present yet (Phase F is mid-rollout —
that table ports later). Once marketplace_plugins is ported, the
soft-fail branch becomes dead code and can be removed.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.engine import Engine

# Re-export the canonical exception class from the DuckDB module so callers
# can catch one ``SystemGroupProtected`` regardless of which backend is
# active. Both implementations raise the same class.
from src.repositories.user_groups import SystemGroupProtected  # noqa: F401


class UserGroupsPgRepository:
    _SELECT_COLS = "id, name, description, is_system, created_at, created_by"

    def __init__(self, engine: Engine):
        self._engine = engine

    def list_all(self) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(f"SELECT {self._SELECT_COLS} FROM user_groups ORDER BY name")
            ).mappings().all()
        return [dict(r) for r in rows]

    def get(self, group_id: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(f"SELECT {self._SELECT_COLS} FROM user_groups WHERE id = :id"),
                {"id": group_id},
            ).mappings().first()
        return dict(row) if row else None

    def get_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(f"SELECT {self._SELECT_COLS} FROM user_groups WHERE name = :name"),
                {"name": name},
            ).mappings().first()
        return dict(row) if row else None

    def create(
        self,
        name: str,
        description: Optional[str] = None,
        created_by: Optional[str] = None,
        is_system: bool = False,
    ) -> Dict[str, Any]:
        group_id = uuid4().hex
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """INSERT INTO user_groups
                       (id, name, description, is_system, created_at, created_by)
                       VALUES (:id, :name, :description, :is_system, :created_at, :created_by)"""
                ),
                {
                    "id": group_id,
                    "name": name,
                    "description": description,
                    "is_system": is_system,
                    "created_at": datetime.now(timezone.utc),
                    "created_by": created_by,
                },
            )

        # Soft-fail fanout — marketplace_plugins table may not be migrated
        # yet during Phase F rollout.
        try:
            from src.repositories.resource_grants_pg import ResourceGrantsPgRepository
            ResourceGrantsPgRepository(self._engine).fanout_system_for_group(
                group_id, assigned_by=created_by,
            )
        except Exception:
            pass

        return self.get(group_id)  # type: ignore[return-value]

    def ensure(
        self, name: str, description: Optional[str] = None
    ) -> Dict[str, Any]:
        existing = self.get_by_name(name)
        if existing:
            return existing
        return self.create(
            name=name,
            description=description or "Auto-created from Google Workspace claim",
            created_by="system:google-sync",
        )

    def ensure_system(self, name: str, description: str) -> Dict[str, Any]:
        existing = self.get_by_name(name)
        if existing:
            if not existing.get("is_system"):
                with self._engine.begin() as conn:
                    conn.execute(
                        sa.text("UPDATE user_groups SET is_system = TRUE WHERE id = :id"),
                        {"id": existing["id"]},
                    )
                existing = self.get(existing["id"])  # type: ignore[assignment]
            return existing  # type: ignore[return-value]
        return self.create(name=name, description=description, is_system=True)

    def update(
        self,
        group_id: str,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
    ) -> None:
        existing = self.get(group_id)
        if (
            existing
            and existing.get("is_system")
            and name is not None
            and name != existing["name"]
        ):
            raise SystemGroupProtected(
                f"group {existing.get('name')!r} is a system group and cannot be renamed"
            )
        sets: List[str] = []
        params: Dict[str, Any] = {"id": group_id}
        if name is not None:
            sets.append("name = :name")
            params["name"] = name
        if description is not None:
            sets.append("description = :description")
            params["description"] = description
        if not sets:
            return
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(f"UPDATE user_groups SET {', '.join(sets)} WHERE id = :id"),
                params,
            )

    def delete(self, group_id: str) -> None:
        existing = self.get(group_id)
        if existing and existing.get("is_system"):
            raise SystemGroupProtected(
                f"group {existing.get('name')!r} is a system group and cannot be deleted"
            )
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM user_groups WHERE id = :id"),
                {"id": group_id},
            )
