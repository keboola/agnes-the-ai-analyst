"""Repository for ``semantic_sources`` (v116) — sync configuration for one
semantic-layer origin (a git repo, an upload, or an existing connection).

``record_sync`` writes ``last_sync_at`` / ``last_sync_status`` /
``last_sync_error`` in one statement so a successful sync can never leave a
stale error behind — an admin page reading the row after a fresh success
must never see the previous failure.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import duckdb


class SemanticSourcesRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    _COLS = [
        "id",
        "kind",
        "name",
        "adapter",
        "config",
        "enabled",
        "last_sync_at",
        "last_sync_status",
        "last_sync_error",
        "created_at",
        "updated_at",
    ]
    _SELECT = ", ".join(_COLS)

    def _decode(self, row) -> Optional[Dict[str, Any]]:
        if not row:
            return None
        out = dict(zip(self._COLS, row))
        val = out.get("config")
        if isinstance(val, str):
            out["config"] = json.loads(val) if val else None
        return out

    def create(
        self,
        *,
        id: str,
        kind: str,
        name: str,
        adapter: str,
        config: Dict[str, Any],
        enabled: bool = True,
    ) -> Dict[str, Any]:
        self.conn.execute(
            f"INSERT INTO semantic_sources ({self._SELECT}) VALUES "
            "(?,?,?,?,?,?,NULL,NULL,NULL,current_timestamp,current_timestamp)",
            [id, kind, name, adapter, json.dumps(config), enabled],
        )
        return self.get(id)  # type: ignore[return-value]

    def get(self, source_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(f"SELECT {self._SELECT} FROM semantic_sources WHERE id = ?", [source_id]).fetchone()
        return self._decode(row)

    def list_all(self, *, enabled_only: bool = False) -> List[Dict[str, Any]]:
        sql = f"SELECT {self._SELECT} FROM semantic_sources"
        if enabled_only:
            sql += " WHERE enabled = TRUE"
        sql += " ORDER BY name"
        return [self._decode(r) for r in self.conn.execute(sql).fetchall()]

    def update(self, source_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
        if not fields:
            return self.get(source_id)
        set_cols = []
        params: List[Any] = []
        for col, val in fields.items():
            set_cols.append(f"{col} = ?")
            params.append(json.dumps(val) if col == "config" else val)
        params.append(source_id)
        self.conn.execute(
            f"UPDATE semantic_sources SET {', '.join(set_cols)}, updated_at = current_timestamp WHERE id = ?",
            params,
        )
        return self.get(source_id)

    def delete(self, source_id: str) -> bool:
        existed = self.get(source_id) is not None
        self.conn.execute("DELETE FROM semantic_sources WHERE id = ?", [source_id])
        return existed

    def record_sync(self, source_id: str, *, status: str, error: Optional[str]) -> None:
        self.conn.execute(
            "UPDATE semantic_sources SET last_sync_at = current_timestamp, "
            "last_sync_status = ?, last_sync_error = ?, updated_at = current_timestamp "
            "WHERE id = ?",
            [status, error, source_id],
        )
