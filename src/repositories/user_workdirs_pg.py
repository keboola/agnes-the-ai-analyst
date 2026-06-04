"""Postgres-backed per-user workdir-marker repository.

Mirrors the ``user_workdirs`` operations of
``app/chat/persistence.py::ChatRepository``. Returns ``app.chat.types``
dataclasses so ChatRepository can delegate transparently.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.chat.types import UserWorkdir


class UserWorkdirPgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    def get_workdir(self, user_email: str) -> Optional[UserWorkdir]:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT user_email, last_init_at, marketplace_sha, "
                    "initial_workspace_sha, agnes_version_at_init "
                    "FROM user_workdirs WHERE user_email = :ue"
                ),
                {"ue": user_email},
            ).mappings().first()
        if not row:
            return None
        return UserWorkdir(
            user_email=row["user_email"],
            last_init_at=row["last_init_at"],
            marketplace_sha=row["marketplace_sha"],
            initial_workspace_sha=row["initial_workspace_sha"],
            agnes_version_at_init=row["agnes_version_at_init"],
        )

    def upsert_workdir(
        self,
        *,
        user_email: str,
        marketplace_sha: Optional[str],
        initial_workspace_sha: Optional[str],
        agnes_version: str,
    ) -> None:
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO user_workdirs "
                    "(user_email, last_init_at, marketplace_sha, "
                    "initial_workspace_sha, agnes_version_at_init) "
                    "VALUES (:ue, :now, :mp, :iw, :ver) "
                    "ON CONFLICT (user_email) DO UPDATE SET "
                    "last_init_at = EXCLUDED.last_init_at, "
                    "marketplace_sha = EXCLUDED.marketplace_sha, "
                    "initial_workspace_sha = EXCLUDED.initial_workspace_sha, "
                    "agnes_version_at_init = EXCLUDED.agnes_version_at_init"
                ),
                {
                    "ue": user_email,
                    "now": now,
                    "mp": marketplace_sha,
                    "iw": initial_workspace_sha,
                    "ver": agnes_version,
                },
            )

    def delete_workdir_row(self, user_email: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM user_workdirs WHERE user_email = :ue"),
                {"ue": user_email},
            )
