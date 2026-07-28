"""Postgres-backed repository for ``agents`` (v103).

Mirrors ``src/repositories/agents.py`` (the DuckDB impl) on the
``AgentsRepository`` public surface. Cross-engine parity is covered by
``tests/db_pg/test_agents_contract.py``.
"""

from __future__ import annotations

import json
import secrets
from typing import Any, Dict, List, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from src.repositories.agents import DEFAULT_SURFACES, decode_json_column


class AgentsPgRepository:
    """Postgres twin of ``AgentsRepository``."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    #: Kept in lock-step with the DuckDB twin's ``_MUTABLE``.
    _MUTABLE = (
        "name",
        "role",
        "instructions",
        "tone",
        "greeting",
        "knowledge",
        "plugins",
        "surfaces",
        "status",
    )
    _JSON_COLS = {"knowledge": list, "plugins": list, "surfaces": dict}

    @staticmethod
    def _row(row: Any) -> Dict[str, Any]:
        rec = dict(row)
        rec["knowledge"] = decode_json_column(rec.get("knowledge"), [])
        rec["plugins"] = decode_json_column(rec.get("plugins"), [])
        rec["surfaces"] = decode_json_column(rec.get("surfaces"), dict(DEFAULT_SURFACES))
        return rec

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create(
        self,
        *,
        name: str,
        slug: str,
        created_by: str,
        role: str = "",
        instructions: str = "",
        tone: str = "concise",
        greeting: str = "",
        knowledge: Optional[List[str]] = None,
        plugins: Optional[List[str]] = None,
        surfaces: Optional[Dict[str, bool]] = None,
        status: str = "draft",
    ) -> str:
        """Insert a new agent; returns the generated ``agt_*`` id.

        Raises ``IntegrityError`` on slug collision.
        """
        agent_id = "agt_" + secrets.token_hex(8)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO agents "
                    "(id, slug, name, role, instructions, tone, greeting, "
                    "knowledge, plugins, surfaces, status, created_by) "
                    "VALUES (:id, :slug, :name, :role, :instructions, :tone, :greeting, "
                    ":knowledge, :plugins, :surfaces, :status, :created_by)"
                ),
                {
                    "id": agent_id,
                    "slug": slug,
                    "name": name,
                    "role": role,
                    "instructions": instructions,
                    "tone": tone,
                    "greeting": greeting,
                    "knowledge": json.dumps(knowledge or []),
                    "plugins": json.dumps(plugins or []),
                    "surfaces": json.dumps(surfaces if surfaces is not None else DEFAULT_SURFACES),
                    "status": status,
                    "created_by": created_by,
                },
            )
        return agent_id

    def get(self, agent_id: str, *, include_deleted: bool = False) -> Optional[Dict[str, Any]]:
        """Fetch one agent by id. Returns ``None`` if not found."""
        guard = "" if include_deleted else " AND deleted_at IS NULL"
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text(f"SELECT * FROM agents WHERE id = :id{guard}"),
                    {"id": agent_id},
                )
                .mappings()
                .first()
            )
        return self._row(row) if row else None

    def get_by_slug(self, slug: str, *, include_deleted: bool = False) -> Optional[Dict[str, Any]]:
        """Fetch one agent by slug. Returns ``None`` if not found or soft-deleted."""
        guard = "" if include_deleted else " AND deleted_at IS NULL"
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(f"SELECT id FROM agents WHERE slug = :slug{guard}"),
                {"slug": slug},
            ).first()
        return self.get(row[0], include_deleted=include_deleted) if row else None

    def list(
        self,
        *,
        created_by: Optional[str] = None,
        search: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """List live agents, name-ordered. ``created_by`` scopes to one owner."""
        query = "SELECT * FROM agents WHERE deleted_at IS NULL"
        params: Dict[str, Any] = {}
        if created_by:
            query += " AND created_by = :created_by"
            params["created_by"] = created_by
        if search:
            query += " AND name ILIKE :search"
            params["search"] = f"%{search}%"
        query += " ORDER BY name LIMIT :limit"
        params["limit"] = limit
        with self._engine.connect() as conn:
            rows = conn.execute(sa.text(query), params).mappings().all()
        return [self._row(r) for r in rows]

    def update(self, agent_id: str, **fields: Any) -> bool:
        """Patch mutable columns; returns False if the agent doesn't exist.

        Unknown and server-owned keys are ignored, so a hostile payload can't
        reassign ``created_by`` or ``slug``.
        """
        sets: List[str] = []
        params: Dict[str, Any] = {"id": agent_id}
        for col in self._MUTABLE:
            if col not in fields:
                continue
            value = fields[col]
            if col in self._JSON_COLS:
                value = json.dumps(value if value is not None else self._JSON_COLS[col]())
            sets.append(f"{col} = :{col}")
            params[col] = value
        sets.append("updated_at = CURRENT_TIMESTAMP")
        with self._engine.begin() as conn:
            row = conn.execute(
                sa.text(f"UPDATE agents SET {', '.join(sets)} WHERE id = :id AND deleted_at IS NULL RETURNING 1"),
                params,
            ).first()
        return row is not None

    def soft_delete(self, agent_id: str) -> None:
        """Set ``deleted_at`` to now (also bumps ``updated_at``). Idempotent."""
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE agents SET deleted_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE id = :id"
                ),
                {"id": agent_id},
            )

    def count_for_user(self, user_id: str) -> int:
        """Live agents owned by ``user_id`` (drives the Library's type facet)."""
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT COUNT(*) FROM agents WHERE created_by = :uid AND deleted_at IS NULL"),
                {"uid": user_id},
            ).first()
        return int(row[0]) if row else 0
