"""Repository for view-name ownership across connectors.

Issue #81 Group C — when two connectors register the same view name in the
master analytics DB, the second one used to silently overwrite the first
(last-write-wins). With this repository the orchestrator records the FIRST
source to claim a name and refuses subsequent collisions until the operator
renames one side.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import duckdb


class ViewOwnershipRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def get_owner(self, view_name: str) -> Optional[str]:
        """Return the source_name that owns ``view_name``, or None."""
        row = self.conn.execute(
            "SELECT source_name FROM view_ownership WHERE view_name = ?",
            [view_name],
        ).fetchone()
        return row[0] if row else None

    def get_all(self) -> Dict[str, str]:
        """Return {view_name: source_name} for every registered ownership."""
        rows = self.conn.execute(
            "SELECT view_name, source_name FROM view_ownership"
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    def claim(self, view_name: str, source_name: str) -> bool:
        """Register ``source_name`` as the owner of ``view_name``.

        Returns True if the claim succeeds (either fresh registration or
        re-claim by the same source). Returns False if a different source
        already owns the name — the caller MUST then refuse to create the
        view and surface the collision to operators.
        """
        existing = self.get_owner(view_name)
        if existing is None:
            self.conn.execute(
                "INSERT INTO view_ownership (view_name, source_name, registered_at) "
                "VALUES (?, ?, ?)",
                [view_name, source_name, datetime.now(timezone.utc)],
            )
            return True
        return existing == source_name

    def release(self, view_name: str, source_name: str) -> bool:
        """Drop ownership of ``view_name`` if held by ``source_name``.

        Used during rebuild cleanup when a connector no longer publishes a
        previously-claimed name (e.g. operator renamed the table on the
        upstream side). Returns True if a row was removed.
        """
        before = self.conn.execute(
            "SELECT COUNT(*) FROM view_ownership "
            "WHERE view_name = ? AND source_name = ?",
            [view_name, source_name],
        ).fetchone()[0]
        if before == 0:
            return False
        self.conn.execute(
            "DELETE FROM view_ownership "
            "WHERE view_name = ? AND source_name = ?",
            [view_name, source_name],
        )
        return True

    def reconcile(
        self, current_pairs: List[Tuple[str, str]]
    ) -> List[Tuple[str, str]]:
        """Drop ownerships for (source_name, view_name) pairs no longer
        present in ``current_pairs``. Returns the list of dropped pairs.

        Called at the end of `SyncOrchestrator.rebuild()` so a renamed or
        removed table immediately releases its name; the next rebuild can
        let a different source claim it without operator intervention.
        """
        live = set(current_pairs)
        all_rows = self.conn.execute(
            "SELECT source_name, view_name FROM view_ownership"
        ).fetchall()
        dropped = [
            (src, view) for src, view in all_rows
            if (src, view) not in live
        ]
        for src, view in dropped:
            self.conn.execute(
                "DELETE FROM view_ownership "
                "WHERE source_name = ? AND view_name = ?",
                [src, view],
            )
        return dropped

    def list_for_source(self, source_name: str) -> List[str]:
        """Return all view names owned by ``source_name``."""
        rows = self.conn.execute(
            "SELECT view_name FROM view_ownership "
            "WHERE source_name = ? ORDER BY view_name",
            [source_name],
        ).fetchall()
        return [r[0] for r in rows]
