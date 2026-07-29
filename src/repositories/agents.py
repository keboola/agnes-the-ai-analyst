"""Repository for owner-scoped agent profiles + scope + scope snapshots (v96)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import duckdb

_UPDATABLE = frozenset(
    {
        "name",
        "description",
        "system_prompt",
        "model",
        "token_budget_monthly",
        "plugins_mode",
        "connections_mode",
        "tables_mode",
        "memory_mode",
        "memory_write_mode",
    }
)


class AgentsRepository:
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
        owner_user_id: str,
        name: str,
        slug: str,
        description: Optional[str] = None,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        token_budget_monthly: Optional[int] = None,
        plugins_mode: str = "all",
        connections_mode: str = "all",
        tables_mode: str = "all",
        memory_mode: str = "all",
        memory_write_mode: str = "propose",
        is_default: bool = False,
    ) -> None:
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """INSERT INTO agents
            (id, owner_user_id, name, slug, description, system_prompt, model,
             token_budget_monthly, plugins_mode, connections_mode, tables_mode,
             memory_mode, memory_write_mode, is_default, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                id,
                owner_user_id,
                name,
                slug,
                description,
                system_prompt,
                model,
                token_budget_monthly,
                plugins_mode,
                connections_mode,
                tables_mode,
                memory_mode,
                memory_write_mode,
                is_default,
                now,
                now,
            ],
        )

    def get_by_id(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """Includes soft-deleted rows so slug tombstoning is inspectable."""
        row = self.conn.execute("SELECT * FROM agents WHERE id = ?", [agent_id]).fetchone()
        return self._row_to_dict(row)

    def get_by_slug(self, owner_user_id: str, slug: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM agents WHERE owner_user_id = ? AND slug = ? AND deleted_at IS NULL",
            [owner_user_id, slug],
        ).fetchone()
        return self._row_to_dict(row)

    def list_for_user(self, owner_user_id: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT * FROM agents
            WHERE owner_user_id = ? AND deleted_at IS NULL
            ORDER BY is_default DESC, name""",
            [owner_user_id],
        ).fetchall()
        return self._rows_to_dicts(rows)

    def update(self, agent_id: str, **fields: Any) -> None:
        bad = set(fields) - _UPDATABLE
        if bad:
            raise ValueError(f"cannot update non-whitelisted field(s): {sorted(bad)}")
        if not fields:
            return
        set_clauses = [f"{col} = ?" for col in fields]
        params: List[Any] = list(fields.values())
        set_clauses.append("updated_at = ?")
        params.append(datetime.now(timezone.utc))
        params.append(agent_id)
        self.conn.execute(
            f"UPDATE agents SET {', '.join(set_clauses)} WHERE id = ?",
            params,
        )

    def soft_delete(self, agent_id: str) -> None:
        self.conn.execute(
            "UPDATE agents SET deleted_at = ? WHERE id = ?",
            [datetime.now(timezone.utc), agent_id],
        )

    def get_or_create_default(self, owner_user_id: str) -> Dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM agents WHERE owner_user_id = ? AND is_default AND deleted_at IS NULL",
            [owner_user_id],
        ).fetchone()
        existing = self._row_to_dict(row)
        if existing is not None:
            return existing

        agent_id = str(uuid.uuid4())
        self.create(
            id=agent_id,
            owner_user_id=owner_user_id,
            name="Default",
            slug="default",
            plugins_mode="all",
            connections_mode="all",
            tables_mode="all",
            memory_mode="all",
            memory_write_mode="propose",
            is_default=True,
        )
        return self.get_by_id(agent_id)  # type: ignore[return-value]

    def set_scope(self, agent_id: str, items: List[Tuple[str, str]]) -> None:
        self.conn.execute("DELETE FROM agent_scope WHERE agent_id = ?", [agent_id])
        if items:
            self.conn.executemany(
                "INSERT INTO agent_scope (agent_id, item_type, item_id) VALUES (?, ?, ?)",
                [[agent_id, item_type, item_id] for item_type, item_id in items],
            )

    def get_scope(self, agent_id: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT item_type, item_id FROM agent_scope WHERE agent_id = ? ORDER BY item_type, item_id",
            [agent_id],
        ).fetchall()
        return self._rows_to_dicts(rows)

    def record_scope_snapshot(self, id: str, session_id: str, agent_id: str, effective_scope: str) -> None:
        self.conn.execute(
            """INSERT INTO agent_scope_snapshots
            (id, session_id, agent_id, effective_scope, created_at)
            VALUES (?, ?, ?, ?, ?)""",
            [id, session_id, agent_id, effective_scope, datetime.now(timezone.utc)],
        )

    def list_scope_snapshots(self, session_id: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM agent_scope_snapshots WHERE session_id = ? ORDER BY created_at",
            [session_id],
        ).fetchall()
        return self._rows_to_dicts(rows)
