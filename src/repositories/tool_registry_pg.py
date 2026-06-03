"""Postgres-backed ToolRegistryRepository.

Mirrors ``src/repositories/tool_registry.py``.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine

MATERIALIZE = "materialize"
PASSTHROUGH = "passthrough"
_VALID_MODES = {MATERIALIZE, PASSTHROUGH}


class ToolRegistryPgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    @staticmethod
    def _decode_row(row: Dict[str, Any]) -> Dict[str, Any]:
        d = dict(row)
        for k in ("input_schema", "pii_fields"):
            if d.get(k) is not None and isinstance(d[k], str):
                try:
                    d[k] = json.loads(d[k])
                except (json.JSONDecodeError, TypeError):
                    pass
        return d

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
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """INSERT INTO tool_registry
                       (tool_id, source_id, original_name, exposed_name, mode, table_id,
                        input_schema, description, mutating, pii_fields, rate_limit_pm,
                        schedule, enabled, created_at, updated_at)
                       VALUES (:tool_id, :source_id, :original_name, :exposed_name,
                               :mode, :table_id, :input_schema, :description, :mutating,
                               :pii_fields, :rate_limit_pm, :schedule, :enabled, :now, :now)
                       ON CONFLICT (tool_id) DO UPDATE SET
                           source_id     = EXCLUDED.source_id,
                           original_name = EXCLUDED.original_name,
                           exposed_name  = EXCLUDED.exposed_name,
                           mode          = EXCLUDED.mode,
                           table_id      = EXCLUDED.table_id,
                           input_schema  = EXCLUDED.input_schema,
                           description   = EXCLUDED.description,
                           mutating      = EXCLUDED.mutating,
                           pii_fields    = EXCLUDED.pii_fields,
                           rate_limit_pm = EXCLUDED.rate_limit_pm,
                           schedule      = EXCLUDED.schedule,
                           enabled       = EXCLUDED.enabled,
                           updated_at    = EXCLUDED.updated_at"""
                ),
                {
                    "tool_id": tool_id, "source_id": source_id,
                    "original_name": original_name, "exposed_name": exposed_name,
                    "mode": mode, "table_id": table_id,
                    "input_schema": json.dumps(input_schema) if input_schema is not None else None,
                    "description": description, "mutating": mutating,
                    "pii_fields": json.dumps(pii_fields) if pii_fields is not None else None,
                    "rate_limit_pm": rate_limit_pm, "schedule": schedule,
                    "enabled": enabled, "now": now,
                },
            )

    def get(self, tool_id: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT * FROM tool_registry WHERE tool_id = :tool_id"),
                {"tool_id": tool_id},
            ).mappings().first()
        return self._decode_row(dict(row)) if row else None

    def list_all(self) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text("SELECT * FROM tool_registry ORDER BY source_id, exposed_name")
            ).mappings().all()
        return [self._decode_row(dict(r)) for r in rows]

    def list_for_source(self, source_id: str) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    "SELECT * FROM tool_registry WHERE source_id = :sid ORDER BY exposed_name"
                ),
                {"sid": source_id},
            ).mappings().all()
        return [self._decode_row(dict(r)) for r in rows]

    def list_by_mode(self, mode: str, *, enabled_only: bool = True) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM tool_registry WHERE mode = :mode"
        params: Dict[str, Any] = {"mode": mode}
        if enabled_only:
            sql += " AND enabled = true"
        sql += " ORDER BY source_id, exposed_name"
        with self._engine.connect() as conn:
            rows = conn.execute(sa.text(sql), params).mappings().all()
        return [self._decode_row(dict(r)) for r in rows]

    def list_passthrough_for_groups(self, group_ids: List[str]) -> List[Dict[str, Any]]:
        if not group_ids:
            return []
        placeholders = ", ".join(f":g{i}" for i in range(len(group_ids)))
        params = {f"g{i}": gid for i, gid in enumerate(group_ids)}
        params["mode"] = PASSTHROUGH
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    f"""SELECT DISTINCT t.*
                          FROM tool_registry t
                          JOIN tool_grants g ON g.tool_id = t.tool_id
                         WHERE t.mode = :mode
                           AND t.enabled = true
                           AND g.group_id IN ({placeholders})
                         ORDER BY t.source_id, t.exposed_name"""
                ),
                params,
            ).mappings().all()
        return [self._decode_row(dict(r)) for r in rows]

    def is_granted_to_groups(self, tool_id: str, group_ids: List[str]) -> bool:
        if not group_ids:
            return False
        placeholders = ", ".join(f":g{i}" for i in range(len(group_ids)))
        params = {f"g{i}": gid for i, gid in enumerate(group_ids)}
        params["tool_id"] = tool_id
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    f"SELECT 1 FROM tool_grants "
                    f"WHERE tool_id = :tool_id AND group_id IN ({placeholders}) LIMIT 1"
                ),
                params,
            ).first()
        return row is not None

    def delete(self, tool_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM tool_grants WHERE tool_id = :id"),
                {"id": tool_id},
            )
            conn.execute(
                sa.text("DELETE FROM tool_registry WHERE tool_id = :id"),
                {"id": tool_id},
            )

    def delete_for_source(self, source_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "DELETE FROM tool_grants WHERE tool_id IN "
                    "(SELECT tool_id FROM tool_registry WHERE source_id = :sid)"
                ),
                {"sid": source_id},
            )
            conn.execute(
                sa.text("DELETE FROM tool_registry WHERE source_id = :sid"),
                {"sid": source_id},
            )

    def add_grant(self, tool_id: str, group_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO tool_grants (tool_id, group_id) "
                    "VALUES (:tool_id, :group_id) ON CONFLICT DO NOTHING"
                ),
                {"tool_id": tool_id, "group_id": group_id},
            )

    def remove_grant(self, tool_id: str, group_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "DELETE FROM tool_grants WHERE tool_id = :tool_id AND group_id = :group_id"
                ),
                {"tool_id": tool_id, "group_id": group_id},
            )

    def grants_for_tool(self, tool_id: str) -> List[str]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text("SELECT group_id FROM tool_grants WHERE tool_id = :tool_id"),
                {"tool_id": tool_id},
            ).all()
        return [r[0] for r in rows]
