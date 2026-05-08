"""Repository for the versioned `news_template` table.

Single table with one row per saved version. `version` increases
monotonically per save. `published` distinguishes the active draft
(FALSE) from publicly-visible versions (TRUE). Web reads
`WHERE published = TRUE ORDER BY version DESC LIMIT 1`. The admin UI
can browse all rows.

**Invariant:** at most one row exists with `published = FALSE` at any
time (the "active draft"). Edits while a draft exists update that
draft row in place — saving repeatedly during composition does not
create a flood of versions. `publish_draft` flips its bit; the next
edit creates a new draft on the next version number.

Sanitization happens here (in `save_draft`) using `src.sanitize_news`.
Templates render the stored content via Jinja's `| safe` and trust it.

Pruning: `save_draft` opportunistically deletes rows older than
`PRUNE_THRESHOLD_DAYS` that are NOT the currently-displayed published
version, so dropped drafts and superseded published versions don't
accumulate forever. The current published version is never pruned.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import duckdb

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


def _row_to_dict(row: tuple[Any, ...] | None) -> dict[str, Any] | None:
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


_FULL_COLUMNS = (
    "id, version, intro, content, published, created_at, updated_at, "
    "created_by, published_at, published_by"
)


class NewsTemplateRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    # -- read helpers ---------------------------------------------------

    def get_current_published(self) -> Optional[dict[str, Any]]:
        """Latest published version. Returns None when no published row
        exists (web renders empty state)."""
        row = self.conn.execute(
            f"SELECT {_FULL_COLUMNS} FROM news_template "
            "WHERE published = TRUE ORDER BY version DESC LIMIT 1"
        ).fetchone()
        return _row_to_dict(row)

    def get_active_draft(self) -> Optional[dict[str, Any]]:
        """The single active draft (published = FALSE), if one exists."""
        row = self.conn.execute(
            f"SELECT {_FULL_COLUMNS} FROM news_template "
            "WHERE published = FALSE ORDER BY version DESC LIMIT 1"
        ).fetchone()
        return _row_to_dict(row)

    def get_version(self, version: int) -> Optional[dict[str, Any]]:
        row = self.conn.execute(
            f"SELECT {_FULL_COLUMNS} FROM news_template WHERE version = ?",
            [version],
        ).fetchone()
        return _row_to_dict(row)

    def list_versions(self, *, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        """Versions ordered version DESC. Each row carries a short text
        preview of intro + content for the admin versions table."""
        rows = self.conn.execute(
            f"SELECT {_FULL_COLUMNS} FROM news_template "
            "ORDER BY version DESC LIMIT ? OFFSET ?",
            [limit, offset],
        ).fetchall()
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

    def save_draft(self, *, intro: str, content: str, by: str) -> dict[str, Any]:
        """UPSERT the active draft. Sanitizes BEFORE writing."""
        intro_clean = sanitize(intro)
        content_clean = sanitize(content)
        now = datetime.now(timezone.utc)

        existing = self.get_active_draft()
        if existing is not None:
            self.conn.execute(
                """UPDATE news_template
                   SET intro = ?, content = ?, updated_at = ?
                   WHERE id = ?""",
                [intro_clean, content_clean, now, existing["id"]],
            )
            row = self.conn.execute(
                f"SELECT {_FULL_COLUMNS} FROM news_template WHERE id = ?",
                [existing["id"]],
            ).fetchone()
        else:
            new_id = str(uuid.uuid4())
            next_version = self.conn.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM news_template"
            ).fetchone()[0]
            self.conn.execute(
                """INSERT INTO news_template
                       (id, version, intro, content, published,
                        created_at, updated_at, created_by)
                   VALUES (?, ?, ?, ?, FALSE, ?, ?, ?)""",
                [new_id, next_version, intro_clean, content_clean, now, now, by],
            )
            row = self.conn.execute(
                f"SELECT {_FULL_COLUMNS} FROM news_template WHERE id = ?",
                [new_id],
            ).fetchone()

        self.prune_old()
        return _row_to_dict(row)  # type: ignore[return-value]

    def publish_draft(self, *, by: str) -> dict[str, Any]:
        """Flip the active draft to published. Raises NoDraftError if
        there is no draft."""
        draft = self.get_active_draft()
        if draft is None:
            raise NoDraftError("No active draft to publish.")
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """UPDATE news_template
               SET published = TRUE, published_at = ?, published_by = ?
               WHERE id = ?""",
            [now, by, draft["id"]],
        )
        row = self.conn.execute(
            f"SELECT {_FULL_COLUMNS} FROM news_template WHERE id = ?",
            [draft["id"]],
        ).fetchone()
        return _row_to_dict(row)  # type: ignore[return-value]

    def unpublish(self, *, version: int, by: str) -> dict[str, Any]:
        """Roll back: flip a published row to a draft. Web then falls
        back to the next-highest published version automatically.

        Raises NotFoundError if the version doesn't exist; raises
        AlreadyDraftError if the row was already a draft. The `by`
        argument is recorded in the audit log by the caller (this
        method itself doesn't audit-log).
        """
        target = self.get_version(version)
        if target is None:
            raise NotFoundError(f"version {version} not found")
        if not target["published"]:
            raise AlreadyDraftError(f"version {version} is already a draft")

        # Maintain the at-most-one-draft invariant. If an active draft
        # already exists when we unpublish a published row, refuse —
        # the admin must publish or delete that draft first. (The CLI /
        # API surface returns a 409 in this case.)
        existing_draft = self.get_active_draft()
        if existing_draft is not None:
            raise AlreadyDraftError(
                f"cannot unpublish version {version} while draft "
                f"version {existing_draft['version']} is active"
            )

        self.conn.execute(
            """UPDATE news_template
               SET published = FALSE,
                   published_at = NULL,
                   published_by = NULL
               WHERE id = ?""",
            [target["id"]],
        )
        row = self.conn.execute(
            f"SELECT {_FULL_COLUMNS} FROM news_template WHERE id = ?",
            [target["id"]],
        ).fetchone()
        return _row_to_dict(row)  # type: ignore[return-value]

    # -- prune ----------------------------------------------------------

    def prune_old(self, *, threshold_days: int = PRUNE_THRESHOLD_DAYS) -> int:
        """Delete rows older than `threshold_days` that are NOT the
        currently-displayed published version. Returns the deleted-row
        count. Idempotent; safe to run on every save."""
        # Identify the current-published id so we can spare it regardless
        # of age. NULL when no published version exists yet.
        current = self.conn.execute(
            "SELECT id FROM news_template "
            "WHERE published = TRUE ORDER BY version DESC LIMIT 1"
        ).fetchone()
        current_id = current[0] if current else None

        if current_id is None:
            res = self.conn.execute(
                f"""DELETE FROM news_template
                    WHERE created_at < (current_timestamp - INTERVAL '{threshold_days} days')"""
            )
        else:
            res = self.conn.execute(
                f"""DELETE FROM news_template
                    WHERE created_at < (current_timestamp - INTERVAL '{threshold_days} days')
                      AND id <> ?""",
                [current_id],
            )
        # DuckDB's DELETE doesn't directly expose rowcount on every
        # binding — fetch the count via a separate query for the test
        # path. Cheap because the table stays tiny.
        return int(getattr(res, "rowcount", 0) or 0)


__all__ = [
    "NewsTemplateRepository",
    "NewsTemplateError",
    "NoDraftError",
    "NotFoundError",
    "AlreadyDraftError",
    "PRUNE_THRESHOLD_DAYS",
]
