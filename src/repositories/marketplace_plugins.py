"""Repository for the per-marketplace plugin cache.

Each row is a single plugin listed in a marketplace's
`.claude-plugin/marketplace.json`. The rows are fully derived from the
cloned working copy on disk — treat this table as a cache that is
refreshed on every successful `src.marketplace.sync_one()` call.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

import duckdb


class MarketplacePluginsRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    @staticmethod
    def _row_to_dict(
        columns: List[str], row: tuple
    ) -> Dict[str, Any]:
        d = dict(zip(columns, row))
        for k in ("source_spec", "raw"):
            v = d.get(k)
            if isinstance(v, str):
                try:
                    d[k] = json.loads(v)
                except (ValueError, TypeError):
                    pass
        return d

    def list_for_marketplace(self, marketplace_id: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM marketplace_plugins WHERE marketplace_id = ? ORDER BY name",
            [marketplace_id],
        ).fetchall()
        if not rows:
            return []
        columns = [d[0] for d in self.conn.description]
        return [self._row_to_dict(columns, r) for r in rows]

    def list_all(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM marketplace_plugins ORDER BY marketplace_id, name"
        ).fetchall()
        if not rows:
            return []
        columns = [d[0] for d in self.conn.description]
        return [self._row_to_dict(columns, r) for r in rows]

    def count_by_marketplace(self) -> Dict[str, int]:
        rows = self.conn.execute(
            "SELECT marketplace_id, COUNT(*) FROM marketplace_plugins GROUP BY marketplace_id"
        ).fetchall()
        return {r[0]: int(r[1]) for r in rows}

    def replace_for_marketplace(
        self,
        marketplace_id: str,
        plugins: Iterable[Dict[str, Any]],
    ) -> int:
        """Replace the full plugin set for one marketplace in a single transaction.

        Returns the number of plugins written.
        """
        plugins_list = list(plugins)
        now = datetime.now(timezone.utc)
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "DELETE FROM marketplace_plugins WHERE marketplace_id = ?",
                [marketplace_id],
            )
            for p in plugins_list:
                name = (p.get("name") or "").strip()
                if not name:
                    continue
                source_spec = p.get("source")
                source_type = _classify_source(source_spec)
                author = p.get("author") or {}
                author_name = author.get("name") if isinstance(author, dict) else None
                self.conn.execute(
                    """INSERT INTO marketplace_plugins
                        (marketplace_id, name, description, version, author_name,
                         homepage, category, source_type, source_spec, raw, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    [
                        marketplace_id,
                        name,
                        p.get("description"),
                        p.get("version"),
                        author_name,
                        p.get("homepage"),
                        p.get("category"),
                        source_type,
                        json.dumps(source_spec) if source_spec is not None else None,
                        json.dumps(p),
                        now,
                    ],
                )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return sum(1 for p in plugins_list if (p.get("name") or "").strip())

    def clear_for_marketplace(self, marketplace_id: str) -> None:
        self.conn.execute(
            "DELETE FROM marketplace_plugins WHERE marketplace_id = ?",
            [marketplace_id],
        )


def _classify_source(source: Optional[Any]) -> Optional[str]:
    """Return a coarse label for the `source` field of a plugin entry.

    Matches the Claude Code marketplace spec (code.claude.com/docs/plugin-marketplaces):
    relative-path string, or one of {github, url, git-subdir, npm}.
    """
    if source is None:
        return None
    if isinstance(source, str):
        return "path"
    if isinstance(source, dict):
        t = source.get("source")
        if isinstance(t, str):
            return t
    return "unknown"
