"""Repository for the per-instance welcome-prompt override.

Post-v26 the welcome-template content lives in the consolidated
`instance_templates` table keyed `'welcome'`. This module preserves the
historical `WelcomeTemplateRepository` API (`.get()` / `.set()` / `.reset()`)
so existing callers (welcome renderer, admin endpoints, tests) keep working
without per-call rewrites; internally every method reads/writes the
`instance_templates WHERE key='welcome'` row.

The legacy on-disk shape (`welcome_template` singleton with `id=1`) is preserved
in the returned dict for compatibility — `id` is hard-coded to `1` so existing
templates that bind it as a hidden form field don't 500.
"""

from datetime import datetime, timezone
from typing import Any

import duckdb

_KEY = "welcome"


class WelcomeTemplateRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def get(self) -> dict[str, Any]:
        """Return the welcome-template row. Always exists post-migration;
        content is None when no override is set (= use shipped default)."""
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
        """Clear override; renderer falls back to shipped default."""
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

    # ------------------------------------------------------------------
    # v75 (#622): Git⇄Editor source toggle. `get_meta` is the richer read the
    # /admin/prompts endpoints + the shared resolver use; `get()` stays
    # backward-compatible for legacy callers.
    # ------------------------------------------------------------------

    def get_meta(self) -> dict[str, Any]:
        """Return the full managed-prompt row including the source toggle.

        Shape: ``{content, source_mode, git_path, base_sha, updated_at,
        updated_by}``. ``source_mode`` defaults to ``'editor'`` for any
        legacy row that predates the v75 backfill.
        """
        row = self.conn.execute(
            "SELECT content, source_mode, git_path, base_sha, updated_at, updated_by "
            "FROM instance_templates WHERE key = ?",
            [_KEY],
        ).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO instance_templates (key, content, source_mode) "
                "VALUES (?, NULL, 'editor') ON CONFLICT (key) DO NOTHING",
                [_KEY],
            )
            return {
                "content": None,
                "source_mode": "editor",
                "git_path": None,
                "base_sha": None,
                "updated_at": None,
                "updated_by": None,
            }
        return {
            "content": row[0],
            "source_mode": row[1] or "editor",
            "git_path": row[2],
            "base_sha": row[3],
            "updated_at": row[4],
            "updated_by": row[5],
        }

    def set_source_mode(self, mode: str, *, updated_by: str) -> None:
        """Flip the source toggle. ``'editor'`` does NOT wipe ``content``
        (the editor draft is preserved so toggling back to git and forward
        again is lossless)."""
        if mode not in ("editor", "git"):
            raise ValueError(f"invalid source_mode: {mode!r}")
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """INSERT INTO instance_templates (key, source_mode, updated_at, updated_by)
               VALUES (?, ?, ?, ?)
               ON CONFLICT (key) DO UPDATE SET
                   source_mode = excluded.source_mode,
                   updated_at = excluded.updated_at,
                   updated_by = excluded.updated_by""",
            [_KEY, mode, now, updated_by],
        )

    def bind_git(self, git_path: str, *, base_sha: str | None, updated_by: str) -> None:
        """Switch to git mode bound to ``git_path`` in the IWT clone, stamping
        the originating ``base_sha`` (Slice-2 divergence detection metadata)."""
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """INSERT INTO instance_templates
                   (key, source_mode, git_path, base_sha, updated_at, updated_by)
               VALUES (?, 'git', ?, ?, ?, ?)
               ON CONFLICT (key) DO UPDATE SET
                   source_mode = 'git',
                   git_path = excluded.git_path,
                   base_sha = excluded.base_sha,
                   updated_at = excluded.updated_at,
                   updated_by = excluded.updated_by""",
            [_KEY, git_path, base_sha, now, updated_by],
        )
