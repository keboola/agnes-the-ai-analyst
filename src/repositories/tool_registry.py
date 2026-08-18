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
        for k in ("input_schema", "pii_fields", "projection_map"):
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
        projection_map: Optional[Dict[str, str]] = None,
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
                enabled, projection_map, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                   -- COALESCE, not overwrite: re-registering a tool (a
                   -- reclassify, a schedule change) does not restate the
                   -- projection mapping, and dropping it would send the
                   -- projection back to guessing column names — the failure
                   -- this column exists to end.
                   projection_map = COALESCE(excluded.projection_map, tool_registry.projection_map),
                   updated_at     = excluded.updated_at""",
            [
                tool_id,
                source_id,
                original_name,
                exposed_name,
                mode,
                table_id,
                json.dumps(input_schema) if input_schema is not None else None,
                description,
                mutating,
                json.dumps(pii_fields) if pii_fields is not None else None,
                rate_limit_pm,
                schedule,
                enabled,
                json.dumps(projection_map) if projection_map is not None else None,
                now,
                now,
            ],
        )

    def set_projection_map(self, tool_id: str, projection_map: Optional[Dict[str, str]]) -> None:
        """Set (or clear, with ``None``) which columns the projection reads.

        Separate from ``upsert`` because the admin chooses this AFTER a fetch,
        against the columns the tool actually emitted — at registration time
        nobody knows what they are.
        """
        self.conn.execute(
            "UPDATE tool_registry SET projection_map = ?, updated_at = ? WHERE tool_id = ?",
            [
                json.dumps(projection_map) if projection_map is not None else None,
                datetime.now(timezone.utc),
                tool_id,
            ],
        )

    def get(self, tool_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM tool_registry WHERE tool_id = ?", [tool_id]).fetchone()
        if not row:
            return None
        cols = [d[0] for d in self.conn.description]
        return self._decode_json(dict(zip(cols, row)))

    def list_all(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM tool_registry ORDER BY source_id, exposed_name").fetchall()
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

    def list_passthrough_for_groups(self, group_ids: List[str]) -> List[Dict[str, Any]]:
        """Passthrough tools any of ``group_ids`` is granted on (DISTINCT).

        Empty ``group_ids`` returns empty — by design, since the RBAC layer
        already short-circuits admin. Callers that want admin-sees-all
        should call ``list_by_mode(PASSTHROUGH)`` instead and skip this.
        """
        if not group_ids:
            return []
        placeholders = ",".join("?" * len(group_ids))
        rows = self.conn.execute(
            f"""SELECT DISTINCT t.*
                  FROM tool_registry t
                  JOIN tool_grants g ON g.tool_id = t.tool_id
                 WHERE t.mode = ?
                   AND t.enabled = true
                   AND g.group_id IN ({placeholders})
                 ORDER BY t.source_id, t.exposed_name""",
            [PASSTHROUGH, *group_ids],
        ).fetchall()
        return self._rows_to_dicts(rows)

    def is_granted_to_groups(self, tool_id: str, group_ids: List[str]) -> bool:
        """True iff any of ``group_ids`` is in tool_grants for this tool."""
        if not group_ids:
            return False
        placeholders = ",".join("?" * len(group_ids))
        row = self.conn.execute(
            f"SELECT 1 FROM tool_grants WHERE tool_id = ? AND group_id IN ({placeholders}) LIMIT 1",
            [tool_id, *group_ids],
        ).fetchone()
        return row is not None

    def delete(self, tool_id: str) -> None:
        # One transaction so a concurrent reader never sees the orphan window
        # between the two cascade deletes (tool_grants rows whose parent
        # tool_registry row is already gone). Matches the PG sibling's single
        # ``engine.begin()``.
        self.conn.execute("BEGIN")
        try:
            self.conn.execute("DELETE FROM tool_grants WHERE tool_id = ?", [tool_id])
            self.conn.execute("DELETE FROM tool_registry WHERE tool_id = ?", [tool_id])
            self.conn.execute("COMMIT")
        except Exception:
            try:
                self.conn.execute("ROLLBACK")
            except Exception:
                pass
            raise

    def delete_for_source(self, source_id: str) -> None:
        tool_ids = [
            r[0]
            for r in self.conn.execute("SELECT tool_id FROM tool_registry WHERE source_id = ?", [source_id]).fetchall()
        ]
        for tid in tool_ids:
            self.delete(tid)

    # tool_grants helpers --------------------------------------------------

    def add_grant(self, tool_id: str, group_id: str, allow_mutating: Optional[bool] = None) -> None:
        """Insert-or-update a grant, tri-state on ``allow_mutating``:

        - ``None`` (default) — leave an existing grant's flag UNCHANGED
          (``ON CONFLICT DO NOTHING``, the pre-v120 semantics); a brand-new
          grant lands read-only. This is what routine re-granting callers
          (Keboola sign-in provisioning, connection rollback) must get, or
          every re-run would silently reset an admin's mutating opt-in.
        - explicit ``True``/``False`` — set the flag, updating an existing
          row in place: the flag is part of the grant, so an explicit
          re-grant is the edit path.
        """
        if allow_mutating is None:
            self.conn.execute(
                "INSERT INTO tool_grants (tool_id, group_id, allow_mutating) VALUES (?, ?, FALSE) "
                "ON CONFLICT (tool_id, group_id) DO NOTHING",
                [tool_id, group_id],
            )
            return
        self.conn.execute(
            "INSERT INTO tool_grants (tool_id, group_id, allow_mutating) VALUES (?, ?, ?) "
            "ON CONFLICT (tool_id, group_id) DO UPDATE SET allow_mutating = excluded.allow_mutating",
            [tool_id, group_id, bool(allow_mutating)],
        )

    def remove_grant(self, tool_id: str, group_id: str) -> None:
        self.conn.execute(
            "DELETE FROM tool_grants WHERE tool_id = ? AND group_id = ?",
            [tool_id, group_id],
        )

    def grants_for_tool(self, tool_id: str) -> List[str]:
        rows = self.conn.execute("SELECT group_id FROM tool_grants WHERE tool_id = ?", [tool_id]).fetchall()
        return [r[0] for r in rows]

    def grant_rows_for_tool(self, tool_id: str) -> List[Dict[str, Any]]:
        """Grants with their flags — ``[{"group_id", "allow_mutating"}, ...]``.

        ``grants_for_tool`` (bare group ids) stays for existing callers;
        this is the detail view. NULL ``allow_mutating`` (row predating
        v120) reads as False.
        """
        rows = self.conn.execute(
            "SELECT group_id, COALESCE(allow_mutating, FALSE) FROM tool_grants WHERE tool_id = ?",
            [tool_id],
        ).fetchall()
        return [{"group_id": r[0], "allow_mutating": bool(r[1])} for r in rows]

    def is_mutating_granted_to_groups(self, tool_id: str, group_ids: List[str]) -> bool:
        """True iff any of ``group_ids`` holds a grant on this tool with
        ``allow_mutating=TRUE`` (the v120 opt-in consumed by
        ``app.api.mcp_policy.check_mutating``)."""
        if not group_ids:
            return False
        placeholders = ",".join("?" * len(group_ids))
        row = self.conn.execute(
            f"SELECT 1 FROM tool_grants WHERE tool_id = ? AND group_id IN ({placeholders}) "
            "AND COALESCE(allow_mutating, FALSE) = TRUE LIMIT 1",
            [tool_id, *group_ids],
        ).fetchone()
        return row is not None
