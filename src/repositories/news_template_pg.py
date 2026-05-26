"""Postgres-backed news_template repository.

Mirrors ``src/repositories/news_template.py`` including the typed error
classes (re-exported from the DuckDB module so callers can catch by the
same class regardless of backend).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from src.sanitize_news import sanitize, stripped_text


PRUNE_THRESHOLD_DAYS = 30


class NewsTemplateError(Exception):
    """Base class for typed news-template errors."""


class NoDraftError(NewsTemplateError):
    """Raised by publish_draft() when no active draft exists to publish."""


class NotFoundError(NewsTemplateError):
    """Raised by version-targeted operations when the version is absent."""


class AlreadyDraftError(NewsTemplateError):
    """Raised by unpublish() when the target version is already a draft."""


class VersionConflictError(NewsTemplateError):
    """Raised when an optimistic-lock check (``expected_version``) doesn't
    match the active draft.
    """

    def __init__(self, *, expected: int | None, actual: int | None, actual_by: str | None = None):
        self.expected = expected
        self.actual = actual
        self.actual_by = actual_by
        super().__init__(
            f"version conflict: expected draft v{expected}, active draft is "
            f"v{actual}{f' (by {actual_by})' if actual_by else ''}"
        )


_FULL_COLUMNS = (
    "id, version, intro, content, published, created_at, updated_at, "
    "created_by, published_at, published_by"
)


def _row_to_dict(row) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    return {
        "id": row[0],
        "version": row[1],
        "intro": row[2],
        "content": row[3],
        "published": bool(row[4]),
        "created_at": row[5],
        "updated_at": row[6],
        "created_by": row[7],
        "published_at": row[8],
        "published_by": row[9],
    }


class NewsTemplatePgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    # -- read helpers ---------------------------------------------------

    def get_current_published(self) -> Optional[dict[str, Any]]:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    f"SELECT {_FULL_COLUMNS} FROM news_template "
                    "WHERE published = TRUE ORDER BY version DESC LIMIT 1"
                )
            ).first()
        return _row_to_dict(row)

    def get_active_draft(self) -> Optional[dict[str, Any]]:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    f"SELECT {_FULL_COLUMNS} FROM news_template "
                    "WHERE published = FALSE ORDER BY version DESC LIMIT 1"
                )
            ).first()
        return _row_to_dict(row)

    def get_version(self, version: int) -> Optional[dict[str, Any]]:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(f"SELECT {_FULL_COLUMNS} FROM news_template WHERE version = :v"),
                {"v": version},
            ).first()
        return _row_to_dict(row)

    def list_versions(self, *, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    f"SELECT {_FULL_COLUMNS} FROM news_template "
                    "ORDER BY version DESC LIMIT :limit OFFSET :offset"
                ),
                {"limit": limit, "offset": offset},
            ).all()
        out: list[dict[str, Any]] = []
        for row in rows:
            d = _row_to_dict(row)
            if d is None:
                continue
            d["status"] = "published" if d["published"] else "draft"
            d["intro_preview"] = stripped_text(d["intro"], limit=120)
            d["content_preview"] = stripped_text(d["content"], limit=120)
            out.append(d)
        return out

    # -- write paths ----------------------------------------------------

    def save_draft(
        self,
        *,
        intro: str,
        content: str,
        by: str,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        intro_clean = sanitize(intro)
        content_clean = sanitize(content)
        now = datetime.now(timezone.utc)

        existing = self.get_active_draft()

        if expected_version is not None:
            current = existing["version"] if existing else 0
            if current != expected_version:
                raise VersionConflictError(
                    expected=expected_version,
                    actual=current if existing else None,
                    actual_by=existing["created_by"] if existing else None,
                )

        with self._engine.begin() as conn:
            if existing is not None:
                conn.execute(
                    sa.text(
                        "UPDATE news_template SET intro = :i, content = :c, updated_at = :now "
                        "WHERE id = :id"
                    ),
                    {"i": intro_clean, "c": content_clean, "now": now, "id": existing["id"]},
                )
                row = conn.execute(
                    sa.text(f"SELECT {_FULL_COLUMNS} FROM news_template WHERE id = :id"),
                    {"id": existing["id"]},
                ).first()
            else:
                new_id = str(uuid.uuid4())
                next_version = conn.execute(
                    sa.text("SELECT COALESCE(MAX(version), 0) + 1 FROM news_template")
                ).scalar()
                conn.execute(
                    sa.text(
                        """INSERT INTO news_template
                           (id, version, intro, content, published,
                            created_at, updated_at, created_by)
                           VALUES (:id, :ver, :i, :c, FALSE, :now, :now, :by)"""
                    ),
                    {"id": new_id, "ver": next_version, "i": intro_clean,
                     "c": content_clean, "now": now, "by": by},
                )
                row = conn.execute(
                    sa.text(f"SELECT {_FULL_COLUMNS} FROM news_template WHERE id = :id"),
                    {"id": new_id},
                ).first()

        self.prune_old()
        return _row_to_dict(row)  # type: ignore[return-value]

    def publish_draft(
        self,
        *,
        by: str,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        draft = self.get_active_draft()
        if draft is None:
            raise NoDraftError("No active draft to publish.")
        if expected_version is not None and draft["version"] != expected_version:
            raise VersionConflictError(
                expected=expected_version,
                actual=draft["version"],
                actual_by=draft["created_by"],
            )
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """UPDATE news_template
                       SET published = TRUE, published_at = :now, published_by = :by
                       WHERE id = :id"""
                ),
                {"now": now, "by": by, "id": draft["id"]},
            )
            row = conn.execute(
                sa.text(f"SELECT {_FULL_COLUMNS} FROM news_template WHERE id = :id"),
                {"id": draft["id"]},
            ).first()
        return _row_to_dict(row)  # type: ignore[return-value]

    def unpublish(self, *, version: int, by: str) -> dict[str, Any]:
        target = self.get_version(version)
        if target is None:
            raise NotFoundError(f"version {version} not found")
        if not target["published"]:
            raise AlreadyDraftError(f"version {version} is already a draft")

        existing_draft = self.get_active_draft()
        if existing_draft is not None:
            raise AlreadyDraftError(
                f"cannot unpublish version {version} while draft "
                f"version {existing_draft['version']} is active"
            )

        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """UPDATE news_template
                       SET published = FALSE,
                           published_at = NULL,
                           published_by = NULL
                       WHERE id = :id"""
                ),
                {"id": target["id"]},
            )
            row = conn.execute(
                sa.text(f"SELECT {_FULL_COLUMNS} FROM news_template WHERE id = :id"),
                {"id": target["id"]},
            ).first()
        return _row_to_dict(row)  # type: ignore[return-value]

    # -- prune ----------------------------------------------------------

    def prune_old(self, *, threshold_days: int = PRUNE_THRESHOLD_DAYS) -> int:
        with self._engine.begin() as conn:
            current = conn.execute(
                sa.text(
                    "SELECT id FROM news_template "
                    "WHERE published = TRUE ORDER BY version DESC LIMIT 1"
                )
            ).first()
            current_id = current[0] if current else None

            if current_id is None:
                res = conn.execute(
                    sa.text(
                        f"DELETE FROM news_template "
                        f"WHERE created_at < (CURRENT_TIMESTAMP - INTERVAL '{threshold_days} days')"
                    )
                )
            else:
                res = conn.execute(
                    sa.text(
                        f"DELETE FROM news_template "
                        f"WHERE created_at < (CURRENT_TIMESTAMP - INTERVAL '{threshold_days} days') "
                        f"  AND id <> :curr"
                    ),
                    {"curr": current_id},
                )
        return int(getattr(res, "rowcount", 0) or 0)


__all__ = [
    "NewsTemplatePgRepository",
    "NewsTemplateError",
    "NoDraftError",
    "NotFoundError",
    "AlreadyDraftError",
    "VersionConflictError",
    "PRUNE_THRESHOLD_DAYS",
]
