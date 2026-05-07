"""Repository for the per-instance CLAUDE.md template override.

Post-v26 the claude_md content lives in the consolidated `instance_templates`
table keyed `'claude_md'`. This module preserves the historical
`ClaudeMdTemplateRepository` API so existing callers (the agnes-init
CLAUDE.md renderer, admin endpoints, tests) keep working without per-call
rewrites; internally every method reads/writes the
`instance_templates WHERE key='claude_md'` row.

The legacy on-disk shape (`claude_md_template` singleton with `id=1`) is
preserved in the returned dict for compatibility — `id` is hard-coded to `1`.
"""

from datetime import datetime, timezone
from typing import Any

import duckdb

_KEY = "claude_md"


class ClaudeMdTemplateRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def get(self) -> dict[str, Any]:
        """Return the claude_md row. Always exists post-migration; content
        is None when no override is set (= use shipped default template)."""
        row = self.conn.execute(
            "SELECT content, updated_at, updated_by FROM instance_templates WHERE key = ?",
            [_KEY],
        ).fetchone()
        if row is None:
            # Defensive: re-seed if a previous admin manually deleted it.
            self.conn.execute(
                "INSERT INTO instance_templates (key, content) VALUES (?, NULL) "
                "ON CONFLICT (key) DO NOTHING",
                [_KEY],
            )
            return {"id": 1, "content": None, "updated_at": None, "updated_by": None}
        return {
            "id": 1,
            "content": row[0],
            "updated_at": row[1],
            "updated_by": row[2],
        }

    def set(self, content: str, *, updated_by: str) -> None:
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """INSERT INTO instance_templates (key, content, updated_at, updated_by)
               VALUES (?, ?, ?, ?)
               ON CONFLICT (key) DO UPDATE SET
                   previous_content = instance_templates.content,
                   content = excluded.content,
                   updated_at = excluded.updated_at,
                   updated_by = excluded.updated_by""",
            [_KEY, content, now, updated_by],
        )

    def reset(self, *, updated_by: str) -> None:
        """Clear override; renderer falls back to shipped default template."""
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """UPDATE instance_templates
               SET previous_content = content,
                   content = NULL,
                   updated_at = ?,
                   updated_by = ?
               WHERE key = ?""",
            [now, updated_by, _KEY],
        )
