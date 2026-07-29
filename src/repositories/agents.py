"""DuckDB-backed repository for ``agents`` (v103).

An agent is an assistant the caller composes in the Agent builder (/agents):
an identity (name/role/instructions/tone/greeting), the knowledge sources and
plugins it may reach, and the surfaces it answers on. Before v103 these lived
in the browser's ``localStorage`` only; the registry makes them real Library
items that can be shared through ``resource_grants``.

The ``knowledge``, ``plugins`` and ``surfaces`` columns are JSON payloads the
builder owns — this layer stores and returns them decoded, so callers work
with lists/dicts and never see the wire format.

Template: src/repositories/file_corpora.py.
"""

from __future__ import annotations

import json
import secrets
from typing import Any, Dict, List, Optional

import duckdb

#: Surfaces an agent can answer on. Web chat is the always-on baseline.
DEFAULT_SURFACES: Dict[str, bool] = {
    "web": True,
    "slack": False,
    "telegram": False,
    "cli": False,
    "mcp": False,
}


def decode_json_column(raw: Any, fallback: Any) -> Any:
    """Decode a JSON column, tolerating NULL/blank/corrupt payloads.

    An agent row with an unreadable payload still renders (with the empty
    fallback) rather than breaking the whole Library listing. Shared with the
    Postgres twin so both engines coerce identically.
    """
    if raw is None or raw == "":
        return fallback
    if isinstance(raw, (list, dict)):
        return raw  # already decoded (PG JSON column / test fixture)
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return fallback
    return decoded if isinstance(decoded, type(fallback)) else fallback


class AgentsRepository:
    """DuckDB twin for the ``agents`` table."""

    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn = conn

    _COLS = [
        "id",
        "slug",
        "name",
        "role",
        "instructions",
        "tone",
        "greeting",
        "knowledge",
        "plugins",
        "surfaces",
        "status",
        "created_by",
        "created_at",
        "updated_at",
        "deleted_at",
    ]
    _SELECT = ", ".join(_COLS)

    #: Columns a caller may PATCH. ``slug``/``created_by``/timestamps are
    #: server-owned, so an update payload can never reassign ownership.
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

    def _row(self, row: Any) -> Dict[str, Any]:
        rec = dict(zip(self._COLS, row))
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

        Raises ``duckdb.ConstraintException`` if ``slug`` collides.
        """
        agent_id = "agt_" + secrets.token_hex(8)
        self.conn.execute(
            "INSERT INTO agents (id, slug, name, role, instructions, tone, greeting, "
            "knowledge, plugins, surfaces, status, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                agent_id,
                slug,
                name,
                role,
                instructions,
                tone,
                greeting,
                json.dumps(knowledge or []),
                json.dumps(plugins or []),
                json.dumps(surfaces if surfaces is not None else DEFAULT_SURFACES),
                status,
                created_by,
            ],
        )
        return agent_id

    def get(self, agent_id: str, *, include_deleted: bool = False) -> Optional[Dict[str, Any]]:
        """Fetch one agent by id. Returns ``None`` if not found."""
        guard = "" if include_deleted else " AND deleted_at IS NULL"
        row = self.conn.execute(
            f"SELECT {self._SELECT} FROM agents WHERE id = ?{guard}",
            [agent_id],
        ).fetchone()
        return self._row(row) if row else None

    def get_by_slug(self, slug: str, *, include_deleted: bool = False) -> Optional[Dict[str, Any]]:
        """Fetch one agent by slug. Returns ``None`` if not found or soft-deleted."""
        guard = "" if include_deleted else " AND deleted_at IS NULL"
        row = self.conn.execute(
            f"SELECT id FROM agents WHERE slug = ?{guard}",
            [slug],
        ).fetchone()
        return self.get(row[0], include_deleted=include_deleted) if row else None

    def list(
        self,
        *,
        created_by: Optional[str] = None,
        search: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """List live agents, name-ordered. ``created_by`` scopes to one owner."""
        query = f"SELECT {self._SELECT} FROM agents WHERE deleted_at IS NULL"
        params: List[Any] = []
        if created_by:
            query += " AND created_by = ?"
            params.append(created_by)
        if search:
            query += " AND name ILIKE ?"
            params.append(f"%{search}%")
        query += " ORDER BY name LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(query, params).fetchall()
        return [self._row(r) for r in rows]

    def update(self, agent_id: str, **fields: Any) -> bool:
        """Patch mutable columns; returns False if the agent doesn't exist.

        Unknown and server-owned keys are ignored, so a hostile payload can't
        reassign ``created_by`` or ``slug``. A call with no recognised field
        still bumps ``updated_at`` (an idempotent touch).
        """
        sets: List[str] = []
        params: List[Any] = []
        for col in self._MUTABLE:
            if col not in fields:
                continue
            value = fields[col]
            if col in self._JSON_COLS:
                value = json.dumps(value if value is not None else self._JSON_COLS[col]())
            sets.append(f"{col} = ?")
            params.append(value)
        sets.append("updated_at = current_timestamp")
        params.append(agent_id)
        # RETURNING 1 (same convention as ResourceGrantsRepository.delete) is
        # how "did this touch a live row" is read — no separate existence SELECT.
        res = self.conn.execute(
            f"UPDATE agents SET {', '.join(sets)} WHERE id = ? AND deleted_at IS NULL RETURNING 1",
            params,
        ).fetchone()
        return res is not None

    def soft_delete(self, agent_id: str) -> None:
        """Set ``deleted_at`` to now (also bumps ``updated_at``). Idempotent."""
        self.conn.execute(
            "UPDATE agents SET deleted_at = current_timestamp, updated_at = current_timestamp WHERE id = ?",
            [agent_id],
        )

    def count_for_user(self, user_id: str) -> int:
        """Live agents owned by ``user_id`` (drives the Library's type facet)."""
        row = self.conn.execute(
            "SELECT COUNT(*) FROM agents WHERE created_by = ? AND deleted_at IS NULL",
            [user_id],
        ).fetchone()
        return int(row[0]) if row else 0
