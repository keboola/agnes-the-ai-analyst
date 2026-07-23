"""Repository for agent-run artifact metadata (v97, agent-api V1b).

Each row is metadata only — the blob itself lives in the object store
under ``object_key``; this table backs listing + download-redirect
lookups scoped to a chat session.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import duckdb


class AgentArtifactsRepository:
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
        session_id: str,
        agent_id: Optional[str],
        owner_user_id: str,
        filename: str,
        object_key: str,
        size_bytes: int,
        content_type: Optional[str],
        md5: Optional[str],
    ) -> None:
        self.conn.execute(
            """INSERT INTO agent_artifacts
            (id, session_id, agent_id, owner_user_id, filename, object_key,
             size_bytes, content_type, md5, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                id,
                session_id,
                agent_id,
                owner_user_id,
                filename,
                object_key,
                size_bytes,
                content_type,
                md5,
                datetime.now(timezone.utc),
            ],
        )

    def get(self, id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM agent_artifacts WHERE id = ?", [id]).fetchone()
        return self._row_to_dict(row)

    def list_for_session(self, session_id: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM agent_artifacts WHERE session_id = ? ORDER BY created_at",
            [session_id],
        ).fetchall()
        return self._rows_to_dicts(rows)
