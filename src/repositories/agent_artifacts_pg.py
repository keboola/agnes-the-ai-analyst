"""Postgres-backed repository for ``agent_artifacts`` (v97, agent-api V1b).

Mirrors ``src/repositories/agent_artifacts.py`` (the DuckDB impl) on the
``AgentArtifactsRepository`` public surface. Cross-engine parity is
covered by ``tests/db_pg/test_agent_artifacts_contract.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine


class AgentArtifactsPgRepository:
    """Postgres twin of ``AgentArtifactsRepository``."""

    def __init__(self, engine: Engine):
        self._engine = engine

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
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """
                    INSERT INTO agent_artifacts
                      (id, session_id, agent_id, owner_user_id, filename, object_key,
                       size_bytes, content_type, md5, created_at)
                    VALUES
                      (:id, :session_id, :agent_id, :owner_user_id, :filename, :object_key,
                       :size_bytes, :content_type, :md5, :created_at)
                    """
                ),
                {
                    "id": id,
                    "session_id": session_id,
                    "agent_id": agent_id,
                    "owner_user_id": owner_user_id,
                    "filename": filename,
                    "object_key": object_key,
                    "size_bytes": size_bytes,
                    "content_type": content_type,
                    "md5": md5,
                    "created_at": datetime.now(timezone.utc),
                },
            )

    def get(self, id: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = conn.execute(sa.text("SELECT * FROM agent_artifacts WHERE id = :id"), {"id": id}).mappings().first()
        return dict(row) if row else None

    def list_for_session(self, session_id: str) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text("SELECT * FROM agent_artifacts WHERE session_id = :session_id ORDER BY created_at"),
                    {"session_id": session_id},
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    def list_for_agent(self, agent_id: str) -> List[Dict[str, Any]]:
        """See `AgentArtifactsRepository.list_for_agent`'s docstring."""
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text("SELECT * FROM agent_artifacts WHERE agent_id = :agent_id ORDER BY created_at"),
                    {"agent_id": agent_id},
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    def delete_for_agent(self, agent_id: str) -> None:
        """See `AgentArtifactsRepository.delete_for_agent`'s docstring."""
        with self._engine.begin() as conn:
            conn.execute(sa.text("DELETE FROM agent_artifacts WHERE agent_id = :agent_id"), {"agent_id": agent_id})
