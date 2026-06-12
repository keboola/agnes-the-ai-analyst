"""Postgres-backed claude_md template repository.

Mirrors ``src/repositories/claude_md_template.py``. Storage is the
shared ``instance_templates`` table keyed ``'claude_md'``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine


_KEY = "claude_md"


class ClaudeMdTemplatePgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    def get(self) -> dict[str, Any]:
        with self._engine.begin() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT content, updated_at, updated_by FROM instance_templates WHERE key = :k"
                ),
                {"k": _KEY},
            ).first()
            if row is None:
                conn.execute(
                    sa.text(
                        "INSERT INTO instance_templates (key, content) VALUES (:k, NULL) "
                        "ON CONFLICT (key) DO NOTHING"
                    ),
                    {"k": _KEY},
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
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """INSERT INTO instance_templates (key, content, updated_at, updated_by)
                       VALUES (:k, :content, :now, :ub)
                       ON CONFLICT (key) DO UPDATE SET
                         previous_content = instance_templates.content,
                         content = EXCLUDED.content,
                         updated_at = EXCLUDED.updated_at,
                         updated_by = EXCLUDED.updated_by"""
                ),
                {"k": _KEY, "content": content, "now": now, "ub": updated_by},
            )

    def reset(self, *, updated_by: str) -> None:
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """UPDATE instance_templates
                       SET previous_content = content,
                           content = NULL,
                           updated_at = :now,
                           updated_by = :ub
                       WHERE key = :k"""
                ),
                {"now": now, "ub": updated_by, "k": _KEY},
            )

    # ------------------------------------------------------------------
    # v75 (#622): Git⇄Editor source toggle. Mirrors the DuckDB sibling.
    # ------------------------------------------------------------------

    def get_meta(self) -> dict[str, Any]:
        with self._engine.begin() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT content, source_mode, git_path, base_sha, updated_at, updated_by "
                    "FROM instance_templates WHERE key = :k"
                ),
                {"k": _KEY},
            ).first()
            if row is None:
                conn.execute(
                    sa.text(
                        "INSERT INTO instance_templates (key, content, source_mode) "
                        "VALUES (:k, NULL, 'editor') ON CONFLICT (key) DO NOTHING"
                    ),
                    {"k": _KEY},
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
        if mode not in ("editor", "git"):
            raise ValueError(f"invalid source_mode: {mode!r}")
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """INSERT INTO instance_templates (key, source_mode, updated_at, updated_by)
                       VALUES (:k, :mode, :now, :ub)
                       ON CONFLICT (key) DO UPDATE SET
                           source_mode = EXCLUDED.source_mode,
                           updated_at = EXCLUDED.updated_at,
                           updated_by = EXCLUDED.updated_by"""
                ),
                {"k": _KEY, "mode": mode, "now": now, "ub": updated_by},
            )

    def bind_git(self, git_path: str, *, base_sha: str | None, updated_by: str) -> None:
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """INSERT INTO instance_templates
                           (key, source_mode, git_path, base_sha, updated_at, updated_by)
                       VALUES (:k, 'git', :gp, :sha, :now, :ub)
                       ON CONFLICT (key) DO UPDATE SET
                           source_mode = 'git',
                           git_path = EXCLUDED.git_path,
                           base_sha = EXCLUDED.base_sha,
                           updated_at = EXCLUDED.updated_at,
                           updated_by = EXCLUDED.updated_by"""
                ),
                {"k": _KEY, "gp": git_path, "sha": base_sha, "now": now, "ub": updated_by},
            )
