"""Repository for `source_connections` (v74) — named data-source connections.

Spec: docs/superpowers/specs/2026-06-12-named-source-connections-design.md.
`config` is stored as a JSON string and returned as a dict. `is_default`
is unique per source_type — enforced here (both backends), not by the DB.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import duckdb


class SourceConnectionsRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def _row_to_dict(self, row: Any, cols: list) -> Optional[Dict[str, Any]]:
        if not row:
            return None
        d = dict(zip(cols, row))
        if isinstance(d.get("config"), str):
            try:
                d["config"] = json.loads(d["config"])
            except (json.JSONDecodeError, TypeError):
                pass
        return d

    def _fetch_one(self, sql: str, params: list) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(sql, params).fetchone()
        cols = [d[0] for d in self.conn.description] if row else []
        return self._row_to_dict(row, cols)

    def create(
        self,
        *,
        id: str,
        name: str,
        source_type: str,
        config: Dict[str, Any],
        token_env: Optional[str] = None,
        is_default: bool = False,
        created_by: Optional[str] = None,
    ) -> None:
        # Wrap the default-demotion UPDATE + INSERT in one transaction so a
        # mid-way failure can't leave the old default demoted with no new row
        # inserted — matches the PG sibling's engine.begin() atomicity.
        self.conn.execute("BEGIN")
        try:
            if is_default:
                self.conn.execute(
                    "UPDATE source_connections SET is_default = FALSE WHERE source_type = ?",
                    [source_type],
                )
            self.conn.execute(
                """INSERT INTO source_connections
                   (id, name, source_type, config, token_env, is_default, created_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [id, name, source_type, json.dumps(config), token_env, is_default, created_by],
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def get(self, connection_id: str) -> Optional[Dict[str, Any]]:
        return self._fetch_one("SELECT * FROM source_connections WHERE id = ?", [connection_id])

    def get_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        return self._fetch_one("SELECT * FROM source_connections WHERE name = ?", [name])

    def get_default(self, source_type: str) -> Optional[Dict[str, Any]]:
        return self._fetch_one(
            "SELECT * FROM source_connections WHERE source_type = ? AND is_default ORDER BY created_at LIMIT 1",
            [source_type],
        )

    def list(self, source_type: Optional[str] = None) -> List[Dict[str, Any]]:
        if source_type:
            rows = self.conn.execute(
                "SELECT * FROM source_connections WHERE source_type = ? ORDER BY name",
                [source_type],
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM source_connections ORDER BY name").fetchall()
        cols = [d[0] for d in self.conn.description]
        return [self._row_to_dict(r, cols) for r in rows]  # type: ignore[misc]

    def update(
        self,
        connection_id: str,
        *,
        config: Optional[Dict[str, Any]] = None,
        token_env: Optional[str] = None,
        is_default: Optional[bool] = None,
    ) -> None:
        # Atomic multi-column update — same transaction guarantee as the PG
        # sibling, so a failure between the UPDATEs can't half-apply.
        self.conn.execute("BEGIN")
        try:
            if config is not None:
                self.conn.execute(
                    "UPDATE source_connections SET config = ? WHERE id = ?",
                    [json.dumps(config), connection_id],
                )
            if token_env is not None:
                self.conn.execute(
                    "UPDATE source_connections SET token_env = ? WHERE id = ?",
                    [token_env, connection_id],
                )
            if is_default is not None:
                if is_default:
                    # Promote: demote every other connection of the same
                    # source_type first (is_default is unique per source_type,
                    # enforced here — mirrors create()).
                    row = self.conn.execute(
                        "SELECT source_type FROM source_connections WHERE id = ?",
                        [connection_id],
                    ).fetchone()
                    if row:
                        self.conn.execute(
                            "UPDATE source_connections SET is_default = FALSE WHERE source_type = ?",
                            [row[0]],
                        )
                    self.conn.execute(
                        "UPDATE source_connections SET is_default = TRUE WHERE id = ?",
                        [connection_id],
                    )
                else:
                    self.conn.execute(
                        "UPDATE source_connections SET is_default = FALSE WHERE id = ?",
                        [connection_id],
                    )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def delete(self, connection_id: str) -> None:
        self.conn.execute("DELETE FROM source_connections WHERE id = ?", [connection_id])
