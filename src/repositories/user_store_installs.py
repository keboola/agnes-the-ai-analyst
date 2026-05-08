"""Repository tracking which Store entities each user has installed.

Composite PK ``(user_id, entity_id)``. Install rows are surfaced into the
served Claude Code marketplace by ``src/marketplace_filter.py``.
"""

from __future__ import annotations

from typing import Any, Dict, List

import duckdb


class UserStoreInstallsRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def install(self, user_id: str, entity_id: str) -> bool:
        """Insert idempotently. Returns True iff a new row was created."""
        existing = self.conn.execute(
            "SELECT 1 FROM user_store_installs "
            "WHERE user_id = ? AND entity_id = ?",
            [user_id, entity_id],
        ).fetchone()
        if existing:
            return False
        self.conn.execute(
            "INSERT INTO user_store_installs (user_id, entity_id) "
            "VALUES (?, ?)",
            [user_id, entity_id],
        )
        return True

    def uninstall(self, user_id: str, entity_id: str) -> bool:
        """Returns True iff a row was deleted."""
        before = self.conn.execute(
            "SELECT 1 FROM user_store_installs "
            "WHERE user_id = ? AND entity_id = ?",
            [user_id, entity_id],
        ).fetchone()
        if not before:
            return False
        self.conn.execute(
            "DELETE FROM user_store_installs "
            "WHERE user_id = ? AND entity_id = ?",
            [user_id, entity_id],
        )
        return True

    def list_for_user(self, user_id: str) -> List[Dict[str, Any]]:
        """Joins store_entities so a single round-trip returns everything the
        UI / marketplace builder needs.

        Filters to approved + archived entities:

        * **approved** — current public entries.
        * **archived** — owner soft-deleted (or admin-archived) entries
          that previously-installed users keep getting served. Pulling
          them from the marketplace.zip would silently break a user's
          existing setup; archive intentionally preserves the install.

        **Excluded** — pending / hidden / blocked. A previously-installed
        entity that subsequently gets blocked by guardrail review must
        NOT continue serving until an admin override re-approves it,
        otherwise a known-bad bundle keeps reaching Claude Code.
        """
        rows = self.conn.execute(
            """SELECT
                   se.id, se.owner_user_id, se.owner_username, se.type,
                   se.name, se.description, se.category, se.version,
                   se.photo_path, se.video_url, se.file_size,
                   se.install_count, se.created_at, se.updated_at,
                   se.visibility_status,
                   usi.installed_at
               FROM user_store_installs usi
               JOIN store_entities se ON se.id = usi.entity_id
               WHERE usi.user_id = ?
                 AND se.visibility_status IN ('approved', 'archived')
               ORDER BY usi.installed_at DESC, se.id""",
            [user_id],
        ).fetchall()
        if not rows:
            return []
        columns = [d[0] for d in self.conn.description]
        return [dict(zip(columns, r)) for r in rows]

    def is_installed(self, user_id: str, entity_id: str) -> bool:
        return bool(
            self.conn.execute(
                "SELECT 1 FROM user_store_installs "
                "WHERE user_id = ? AND entity_id = ?",
                [user_id, entity_id],
            ).fetchone()
        )

    def installer_count(self, entity_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM user_store_installs WHERE entity_id = ?",
            [entity_id],
        ).fetchone()
        return int(row[0]) if row else 0

    def delete_all_for_entity(self, entity_id: str) -> int:
        """Used by the entity-delete code path. Returns rows deleted."""
        before = self.conn.execute(
            "SELECT COUNT(*) FROM user_store_installs WHERE entity_id = ?",
            [entity_id],
        ).fetchone()[0]
        self.conn.execute(
            "DELETE FROM user_store_installs WHERE entity_id = ?",
            [entity_id],
        )
        return int(before)
