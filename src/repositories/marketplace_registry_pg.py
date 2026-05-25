"""Postgres-backed marketplace_registry repository.

Mirrors ``src/repositories/marketplace_registry.py``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine


class MarketplaceRegistryPgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    def register(
        self,
        id: str,
        name: str,
        url: str,
        branch: Optional[str] = None,
        token_env: Optional[str] = None,
        description: Optional[str] = None,
        registered_by: Optional[str] = None,
        curator_name: Optional[str] = None,
        curator_email: Optional[str] = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """INSERT INTO marketplace_registry
                        (id, name, url, branch, token_env, description, registered_by,
                         registered_at, curator_name, curator_email)
                    VALUES (:id, :name, :url, :branch, :te, :desc, :rb, :now, :cn, :ce)
                    ON CONFLICT (id) DO UPDATE SET
                        name = EXCLUDED.name,
                        url = EXCLUDED.url,
                        branch = EXCLUDED.branch,
                        token_env = EXCLUDED.token_env,
                        description = EXCLUDED.description,
                        curator_name = COALESCE(EXCLUDED.curator_name, marketplace_registry.curator_name),
                        curator_email = COALESCE(EXCLUDED.curator_email, marketplace_registry.curator_email)"""
                ),
                {
                    "id": id, "name": name, "url": url, "branch": branch,
                    "te": token_env, "desc": description, "rb": registered_by,
                    "now": now, "cn": curator_name, "ce": curator_email,
                },
            )

    def unregister(self, marketplace_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM marketplace_registry WHERE id = :id"),
                {"id": marketplace_id},
            )

    def get(self, marketplace_id: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT * FROM marketplace_registry WHERE id = :id"),
                {"id": marketplace_id},
            ).mappings().first()
        return dict(row) if row else None

    def list_all(self) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text("SELECT * FROM marketplace_registry ORDER BY name")
            ).mappings().all()
        return [dict(r) for r in rows]

    def update_sync_status(
        self,
        marketplace_id: str,
        *,
        commit_sha: Optional[str] = None,
        synced_at: Optional[datetime] = None,
        error: Optional[str] = None,
    ) -> None:
        sets: List[str] = []
        params: Dict[str, Any] = {"id": marketplace_id}
        if synced_at is not None:
            sets.append("last_synced_at = :sa")
            params["sa"] = synced_at
        if commit_sha is not None:
            sets.append("last_commit_sha = :sha")
            params["sha"] = commit_sha
        if commit_sha is not None and error is None:
            sets.append("last_error = NULL")
        elif error is not None:
            sets.append("last_error = :err")
            params["err"] = error
        if not sets:
            return
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    f"UPDATE marketplace_registry SET {', '.join(sets)} WHERE id = :id"
                ),
                params,
            )
