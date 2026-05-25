"""Postgres-backed table profiles repository.

Mirrors ``src/repositories/profiles.py``.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine


class ProfilePgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    def save(self, table_id: str, profile: dict) -> None:
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """INSERT INTO table_profiles (table_id, profile, profiled_at)
                       VALUES (:t, CAST(:p AS JSONB), :now)
                       ON CONFLICT (table_id) DO UPDATE SET
                         profile = EXCLUDED.profile,
                         profiled_at = EXCLUDED.profiled_at"""
                ),
                {"t": table_id, "p": json.dumps(profile), "now": now},
            )

    def get(self, table_id: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT profile, profiled_at FROM table_profiles WHERE table_id = :t"
                ),
                {"t": table_id},
            ).first()
        if not row:
            return None
        profile = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        profile["profiled_at"] = row[1]
        return profile

    def get_all(self) -> Dict[str, dict]:
        out: Dict[str, dict] = {}
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    "SELECT table_id, profile, profiled_at FROM table_profiles ORDER BY table_id"
                )
            ).all()
        for row in rows:
            profile = row[1] if isinstance(row[1], dict) else json.loads(row[1])
            profile["profiled_at"] = row[2]
            out[row[0]] = profile
        return out
