"""Repository for community-uploaded Store entities.

A row represents one skill/agent/plugin that an authenticated user has
uploaded through the ``/store/new`` page or ``POST /api/store/entities``.
The row is the index over the on-disk plugin tree at
``${DATA_DIR}/store/<entity_id>/plugin/`` — see ``app/api/store.py`` for the
upload + bake pipeline.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import duckdb


class StoreEntitiesRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    @staticmethod
    def _row_to_dict(columns: List[str], row: tuple) -> Dict[str, Any]:
        d = dict(zip(columns, row))
        for k in ("doc_paths",):
            v = d.get(k)
            if isinstance(v, str):
                try:
                    d[k] = json.loads(v)
                except (ValueError, TypeError):
                    d[k] = []
            elif v is None:
                d[k] = []
        return d

    def create(
        self,
        *,
        id: str,
        owner_user_id: str,
        owner_username: str,
        type: str,
        name: str,
        description: Optional[str],
        category: Optional[str],
        version: str,
        photo_path: Optional[str] = None,
        video_url: Optional[str] = None,
        doc_paths: Optional[List[str]] = None,
        file_size: int = 0,
        visibility_status: str = "pending",
    ) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """INSERT INTO store_entities
                (id, owner_user_id, owner_username, type, name, description,
                 category, version, photo_path, video_url, doc_paths,
                 file_size, install_count, visibility_status,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)""",
            [
                id, owner_user_id, owner_username, type, name, description,
                category, version, photo_path, video_url,
                json.dumps(doc_paths or []),
                int(file_size), visibility_status, now, now,
            ],
        )
        return self.get(id)  # type: ignore[return-value]

    def set_visibility(self, id: str, status: str) -> None:
        """Flip visibility_status on a submission's resolution.

        Called by the guardrail runner after the LLM review completes
        ('approved' on safe, 'hidden' on review_error/blocked) and by
        the admin override path ('approved' after force-publish).
        Soft-delete uses 'archived' via :meth:`archive` instead of this
        helper so the actor + timestamp are recorded.

        When transitioning OUT of 'archived' (admin override re-publishes
        an archived row), the archive metadata is cleared in the same
        UPDATE so a future read of the row doesn't show stale
        archived_at / archived_by alongside the new status.
        """
        if status not in ("pending", "approved", "hidden", "archived"):
            raise ValueError(f"invalid visibility_status: {status!r}")
        now = datetime.now(timezone.utc)
        if status == "archived":
            # Use :meth:`archive` instead — this branch shouldn't be hit
            # but is left permissive so existing call sites work; the
            # actor + timestamp won't be recorded though.
            self.conn.execute(
                "UPDATE store_entities SET visibility_status = ?, updated_at = ? WHERE id = ?",
                [status, now, id],
            )
            return
        # Transitioning to a non-archived state — null out the archive
        # metadata so old archive forensics don't bleed into the new row
        # state, AND strip the archive-rename suffix so the original
        # display name is restored. Conflict path: if the original name
        # slot was taken by a re-upload, append `-restored-N` so the
        # un-archive doesn't 409.
        from src.store_naming import is_archived_name, strip_archive_suffix
        row = self.get(id)
        new_name = row["name"] if row else None
        if row and is_archived_name(row["name"]):
            stripped = strip_archive_suffix(row["name"])
            owner_id = row["owner_user_id"]
            # Probe for collision against active rows (skip self).
            taken = self.conn.execute(
                "SELECT id FROM store_entities "
                "WHERE owner_user_id = ? AND name = ? AND id != ?",
                [owner_id, stripped, id],
            ).fetchone()
            if not taken:
                new_name = stripped
            else:
                # Find the next free `-restored-N` suffix.
                n = 1
                while True:
                    candidate = f"{stripped}-restored-{n}"
                    clash = self.conn.execute(
                        "SELECT id FROM store_entities "
                        "WHERE owner_user_id = ? AND name = ? AND id != ?",
                        [owner_id, candidate, id],
                    ).fetchone()
                    if not clash:
                        new_name = candidate
                        break
                    n += 1
                    if n > 100:  # paranoid bound
                        new_name = f"{stripped}-restored-{int(now.timestamp())}"
                        break
        self.conn.execute(
            """UPDATE store_entities
                  SET visibility_status = ?,
                      name = ?,
                      archived_at = NULL,
                      archived_by = NULL,
                      updated_at = ?
                WHERE id = ?""",
            [status, new_name, now, id],
        )

    def set_visibility_if_pending(self, id: str, status: str) -> bool:
        """Background-task safe variant of :meth:`set_visibility`.

        Only flips the row when its current ``visibility_status`` is one
        of {'pending', 'hidden'} — i.e. the row is still in the review
        window. Returns ``True`` if the update applied, ``False`` if
        the row was in another state (admin already archived,
        approved-and-installed, or hard-deleted between the BG task's
        start and verdict-write).

        Used by the LLM review runner so a verdict landing late doesn't
        clobber an admin decision (e.g. archive while review was in
        flight).
        """
        if status not in ("pending", "approved", "hidden", "archived"):
            raise ValueError(f"invalid visibility_status: {status!r}")
        # Read-then-update: DuckDB doesn't expose rowcount on the python
        # API in a portable way across versions, so we check first. Both
        # statements run on the same connection, serialized writer, so a
        # concurrent admin archive between SELECT and UPDATE is a
        # vanishingly thin window. The defensive guard in the WHERE
        # clause closes the loop.
        row = self.conn.execute(
            "SELECT visibility_status FROM store_entities WHERE id = ?",
            [id],
        ).fetchone()
        if row is None:
            return False
        current = row[0]
        if current not in ("pending", "hidden"):
            return False
        now = datetime.now(timezone.utc)
        # Re-check via WHERE so an admin archive that landed between the
        # SELECT and the UPDATE doesn't get clobbered.
        self.conn.execute(
            """UPDATE store_entities
                  SET visibility_status = ?,
                      archived_at = NULL,
                      archived_by = NULL,
                      updated_at = ?
                WHERE id = ?
                  AND visibility_status IN ('pending', 'hidden')""",
            [status, now, id],
        )
        # Re-read to confirm the flip applied (admin may have raced in).
        confirm = self.conn.execute(
            "SELECT visibility_status FROM store_entities WHERE id = ?",
            [id],
        ).fetchone()
        return confirm is not None and confirm[0] == status

    def archive(self, id: str, *, by_user_id: str) -> Dict[str, str]:
        """Soft-delete: flip visibility to 'archived' + record actor +
        timestamp + rename the row's ``name`` so the (owner, name) slot
        and the global ``<name>-by-<username>`` suffix slot free up for
        re-upload.

        Returns a dict with ``original_name`` and ``new_name`` so the
        caller (the DELETE handler in ``app/api/store.py``) can rename
        the on-disk skill/agent/plugin subdir + audit-log the original
        name.

        Existing user_store_installs continue to serve the bundle
        through marketplace.zip / .git (filter is approved + archived
        in :class:`UserStoreInstallsRepository.list_for_user`); the
        served slug carries the new suffixed name so consumers see the
        plugin renamed on next sync.

        Idempotent: archiving an already-archived row no-ops (the
        existing suffix is preserved; we don't re-rename).
        """
        from src.store_naming import is_archived_name, make_archive_name
        row = self.get(id)
        if row is None:
            return {"original_name": "", "new_name": ""}
        # Re-archive: keep the existing suffixed name to avoid churning
        # the on-disk path on every redundant archive call.
        if (
            row.get("visibility_status") == "archived"
            and is_archived_name(row.get("name") or "")
        ):
            return {
                "original_name": row.get("name") or "",
                "new_name": row.get("name") or "",
            }

        original = row.get("name") or ""
        now = datetime.now(timezone.utc)
        new_name = make_archive_name(original, int(now.timestamp()))
        self.conn.execute(
            """UPDATE store_entities
                  SET visibility_status = 'archived',
                      name = ?,
                      archived_at = ?,
                      archived_by = ?,
                      updated_at = ?
                WHERE id = ?""",
            [new_name, now, by_user_id, now, id],
        )
        return {"original_name": original, "new_name": new_name}

    def update(
        self,
        id: str,
        *,
        description: Optional[str] = None,
        category: Optional[str] = None,
        version: Optional[str] = None,
        photo_path: Optional[str] = None,
        video_url: Optional[str] = None,
        doc_paths: Optional[List[str]] = None,
        file_size: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """Partial update — only the supplied columns change. Returns the
        updated row, or None if no row matched.
        """
        sets: List[str] = []
        params: List[Any] = []
        if description is not None:
            sets.append("description = ?"); params.append(description)
        if category is not None:
            sets.append("category = ?"); params.append(category)
        if version is not None:
            sets.append("version = ?"); params.append(version)
        if photo_path is not None:
            sets.append("photo_path = ?"); params.append(photo_path)
        if video_url is not None:
            sets.append("video_url = ?"); params.append(video_url)
        if doc_paths is not None:
            sets.append("doc_paths = ?"); params.append(json.dumps(doc_paths))
        if file_size is not None:
            sets.append("file_size = ?"); params.append(int(file_size))
        if not sets:
            return self.get(id)
        sets.append("updated_at = ?"); params.append(datetime.now(timezone.utc))
        params.append(id)
        self.conn.execute(
            f"UPDATE store_entities SET {', '.join(sets)} WHERE id = ?",
            params,
        )
        return self.get(id)

    def get(self, id: str) -> Optional[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM store_entities WHERE id = ?", [id]
        ).fetchall()
        if not rows:
            return None
        columns = [d[0] for d in self.conn.description]
        return self._row_to_dict(columns, rows[0])

    def get_by_owner_and_name(
        self,
        owner_user_id: str,
        name: str,
        *,
        exclude_archived: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Fetch a single entity by (owner, name).

        ``exclude_archived=True`` skips rows whose
        ``visibility_status='archived'`` — used by the upload conflict
        check so a freshly archived prior doesn't block a same-name
        re-upload (the archive flow renames the row to free the slot,
        but skipping here is belt-and-braces).
        """
        sql = "SELECT * FROM store_entities WHERE owner_user_id = ? AND name = ?"
        params: List[Any] = [owner_user_id, name]
        if exclude_archived:
            sql += " AND visibility_status != 'archived'"
        rows = self.conn.execute(sql, params).fetchall()
        if not rows:
            return None
        columns = [d[0] for d in self.conn.description]
        return self._row_to_dict(columns, rows[0])

    def delete(self, id: str) -> None:
        self.conn.execute("DELETE FROM store_entities WHERE id = ?", [id])

    def list(
        self,
        *,
        skip: int = 0,
        limit: int = 24,
        type: Optional[str] = None,
        category: Optional[str] = None,
        search: Optional[str] = None,
        owner_user_id: Optional[str] = None,
        visibility_status: Optional[List[str]] = None,
        include_owner_id: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Filtered + paginated listing.

        Returns ``(items, total)`` where ``total`` is the unfiltered count
        across pages — used to render pagination controls.

        ``visibility_status`` whitelists which guardrail states are visible.
        Non-admin browse passes ``["approved"]``; admin/owner views pass
        ``None`` (no filter) so pending/hidden entries surface in their UIs.

        ``include_owner_id`` is the "show me my own pending stuff too"
        knob: when set alongside a ``visibility_status`` whitelist, the
        SQL becomes ``(visibility IN (...) OR owner_user_id = :uid)`` so
        the caller's own non-approved entries surface in an otherwise
        approved-only listing. Used by the marketplace + store browse
        pages so submitters spot their own under-review uploads in the
        grid.
        """
        clauses: List[str] = []
        params: List[Any] = []
        if type:
            clauses.append("type = ?"); params.append(type)
        if category:
            # "Other" is the synthetic bucket for entities with NULL / empty
            # category (matches what /api/marketplace/categories reports for
            # the Flea tab). An explicit category=='Other' string also lands
            # here so a user who picked "Other" at upload time stays grouped.
            if category == "Other":
                clauses.append(
                    "(category IS NULL OR TRIM(category) = '' OR category = ?)"
                )
                params.append(category)
            else:
                clauses.append("category = ?"); params.append(category)
        if owner_user_id:
            clauses.append("owner_user_id = ?"); params.append(owner_user_id)
        if search:
            clauses.append("(LOWER(name) LIKE ? OR LOWER(description) LIKE ?)")
            like = f"%{search.lower()}%"
            params.extend([like, like])
        if visibility_status:
            placeholders = ",".join("?" for _ in visibility_status)
            if include_owner_id:
                # Approved (or whatever the whitelist allows) for everyone,
                # plus the caller's OWN entries that aren't archived.
                # Archived stays admin-only across browse — even the
                # owner's own archived rows must NOT appear in browse
                # listings (per user direction "only admins should see
                # archived submissions"). My AI Stack uses a different
                # path (user_store_installs.list_for_user) and DOES
                # surface archived for already-installed plugins.
                clauses.append(
                    f"(visibility_status IN ({placeholders}) "
                    f"OR (owner_user_id = ? AND visibility_status != 'archived'))"
                )
                params.extend(visibility_status)
                params.append(include_owner_id)
            else:
                clauses.append(f"visibility_status IN ({placeholders})")
                params.extend(visibility_status)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        total = self.conn.execute(
            f"SELECT COUNT(*) FROM store_entities {where}", params,
        ).fetchone()[0]

        rows = self.conn.execute(
            f"""SELECT * FROM store_entities {where}
                ORDER BY created_at DESC, id
                LIMIT ? OFFSET ?""",
            [*params, int(limit), int(skip)],
        ).fetchall()
        if not rows:
            return [], int(total)
        columns = [d[0] for d in self.conn.description]
        items = [self._row_to_dict(columns, r) for r in rows]
        return items, int(total)

    def bump_install_count(self, id: str, delta: int) -> None:
        """Adjust install_count by delta (signed). Floors at 0 — concurrent
        uninstall + delete races shouldn't push the number negative.
        """
        self.conn.execute(
            "UPDATE store_entities "
            "SET install_count = GREATEST(install_count + ?, 0) "
            "WHERE id = ?",
            [int(delta), id],
        )
