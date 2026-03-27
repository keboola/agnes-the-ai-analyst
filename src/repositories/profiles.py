"""Repository for table profiles."""

import json
from datetime import datetime, timezone
from typing import Any, Optional, Dict

import duckdb


class ProfileRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def save(self, table_id: str, profile: dict) -> None:
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """INSERT INTO table_profiles (table_id, profile, profiled_at)
            VALUES (?, ?, ?)
            ON CONFLICT (table_id) DO UPDATE SET
                profile = excluded.profile, profiled_at = excluded.profiled_at""",
            [table_id, json.dumps(profile), now],
        )

    def get(self, table_id: str) -> Optional[Dict[str, Any]]:
        result = self.conn.execute(
            "SELECT profile, profiled_at FROM table_profiles WHERE table_id = ?",
            [table_id],
        ).fetchone()
        if not result:
            return None
        profile = json.loads(result[0]) if isinstance(result[0], str) else result[0]
        profile["profiled_at"] = result[1]
        return profile

    def get_all(self) -> Dict[str, dict]:
        results = self.conn.execute(
            "SELECT table_id, profile, profiled_at FROM table_profiles ORDER BY table_id"
        ).fetchall()
        out = {}
        for row in results:
            profile = json.loads(row[1]) if isinstance(row[1], str) else row[1]
            profile["profiled_at"] = row[2]
            out[row[0]] = profile
        return out
