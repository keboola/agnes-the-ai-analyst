"""Repository for the per-instance setup-page banner override (singleton row)."""

from datetime import datetime, timezone
from typing import Any, Optional

import duckdb


class SetupBannerRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def get(self) -> dict[str, Any]:
        """Return the singleton row. Always exists post-migration; content
        is None when no banner is set."""
        row = self.conn.execute(
            "SELECT id, content, updated_at, updated_by FROM setup_banner WHERE id = 1"
        ).fetchone()
        if row is None:
            # Defensive: re-seed if a previous admin manually deleted it.
            self.conn.execute(
                "INSERT INTO setup_banner (id, content) VALUES (1, NULL) "
                "ON CONFLICT (id) DO NOTHING"
            )
            return {"id": 1, "content": None, "updated_at": None, "updated_by": None}
        return {
            "id": row[0],
            "content": row[1],
            "updated_at": row[2],
            "updated_by": row[3],
        }

    def set(self, content: str, *, updated_by: str) -> None:
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """INSERT INTO setup_banner (id, content, updated_at, updated_by)
               VALUES (1, ?, ?, ?)
               ON CONFLICT (id) DO UPDATE SET
                   content = excluded.content,
                   updated_at = excluded.updated_at,
                   updated_by = excluded.updated_by""",
            [content, now, updated_by],
        )

    def reset(self, *, updated_by: str) -> None:
        """Clear the banner; /setup will show no banner."""
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """UPDATE setup_banner
               SET content = NULL, updated_at = ?, updated_by = ?
               WHERE id = 1""",
            [now, updated_by],
        )
