"""Repository for `tool_registry` + `tool_grants` (v61) — Universal MCP tools."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import duckdb


MATERIALIZE = "materialize"
PASSTHROUGH = "passthrough"
_VALID_MODES = {MATERIALIZE, PASSTHROUGH}


class ToolRegistryRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    @staticmethod
    def _decode_json(d: Dict[str, Any]) -> Dict[str, Any]:
        for k in ("input_schema", "pii_fields"):
            if d.get(k) is not None and isinstance(d[k], str):
                try:
                    d[k] = json.loads(d[k])
                except (json.JSONDecodeError, TypeError):
                    pass
        return d

    def _rows_to_dicts(self, rows) -> List[Dict[str, Any]]:
        if not rows:
            return []
        cols = [d[0] for d in self.conn.description]
        return [self._decode_json(dict(zip(cols, r))) for r in rows]

    def upsert(
        self,
        *,
        tool_id: str,
        source_id: str,
        original_name: str,
        exposed_name: str,
        mode: str,
        table_id: Optional[str] = None,
        input_schema: Optional[Dict[str, Any]] = None,
        description: Optional[str] = None,
        mutating: bool = False,
        pii_fields: Optional[List[str]] = None,
        rate_limit_pm: Optional[int] = None,
        schedule: Optional[str] = None,
        enabled: bool = True,
    ) -> None:
        if mode not in _VALID_MODES:
            raise ValueError(f"invalid mode: {mode}; must be one of {_VALID_MODES}")
        if mode == MATERIALIZE and not schedule:
            raise ValueError("materialize mode requires a schedule")
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """INSERT INTO tool_registry
               (tool_id, source_id, original_name, exposed_name, mode, table_id,
                input_schema, description, mutating, pii_fields, rate_limit_pm, schedule,
                enabled, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (tool_id) DO UPDATE SET
                   source_id      = excluded.source_id,
                   original_name  = excluded.original_name,
                   exposed_name   = excluded.exposed_name,
                   mode           = excluded.mode,
                   table_id       = excluded.table_id,
                   input_schema   = excluded.input_schema,
                   description    = excluded.description,
                   mutating       = excluded.mutating,
                   pii_fields     = excluded.pii_fields,
                   rate_limit_pm  = excluded.rate_limit_pm,
                   schedule       = excluded.schedule,
                   enabled        = excluded.enabled,
                   updated_at     = excluded.updated_at""",
            [
                tool_id, source_id, original_name, exposed_name, mode, table_id,
                json.dumps(input_schema) if input_schema is not None else None,
                description, mutating,
                json.dumps(pii_fields) if pii_fields is not None else None,
                rate_limit_pm, schedule, enabled, now, now,
            ],
        )

    def get(self, tool_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM tool_registry WHERE tool_id = ?", [tool_id]).fetchone()
        if not row:
            return None
        cols = [d[0] for d in self.conn.description]
        return self._decode_json(dict(zip(cols, row)))

    def list_all(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM tool_registry ORDER BY source_id, exposed_name"
        ).fetchall()
        return self._rows_to_dicts(rows)

    def list_for_source(self, source_id: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM tool_registry WHERE source_id = ? ORDER BY exposed_name",
            [source_id],
        ).fetchall()
        return self._rows_to_dicts(rows)

    def list_by_mode(self, mode: str, *, enabled_only: bool = True) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM tool_registry WHERE mode = ?"
        params: List[Any] = [mode]
        if enabled_only:
            sql += " AND enabled = true"
        sql += " ORDER BY source_id, exposed_name"
        rows = self.conn.execute(sql, params).fetchall()
        return self._rows_to_dicts(rows)

    def delete(self, tool_id: str) -> None:
        self.conn.execute("DELETE FROM tool_grants WHERE tool_id = ?", [tool_id])
        self.conn.execute("DELETE FROM tool_registry WHERE tool_id = ?", [tool_id])

    def delete_for_source(self, source_id: str) -> None:
        tool_ids = [r[0] for r in self.conn.execute(
            "SELECT tool_id FROM tool_registry WHERE source_id = ?", [source_id]
        ).fetchall()]
        for tid in tool_ids:
            self.delete(tid)

    # tool_grants helpers --------------------------------------------------

    def add_grant(self, tool_id: str, group_id: str) -> None:
        self.conn.execute(
            "INSERT INTO tool_grants (tool_id, group_id) VALUES (?, ?) ON CONFLICT DO NOTHING",
            [tool_id, group_id],
        )

    def remove_grant(self, tool_id: str, group_id: str) -> None:
        self.conn.execute(
            "DELETE FROM tool_grants WHERE tool_id = ? AND group_id = ?",
            [tool_id, group_id],
        )

    def grants_for_tool(self, tool_id: str) -> List[str]:
        rows = self.conn.execute(
            "SELECT group_id FROM tool_grants WHERE tool_id = ?", [tool_id]
        ).fetchall()
        return [r[0] for r in rows]
