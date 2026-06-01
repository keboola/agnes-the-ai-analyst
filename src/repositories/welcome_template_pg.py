"""Postgres-backed welcome_template repository.

Mirrors ``src/repositories/welcome_template.py``. Storage is the shared
``instance_templates`` table keyed ``'welcome'``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine


_KEY = "welcome"


class WelcomeTemplatePgRepository:
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
