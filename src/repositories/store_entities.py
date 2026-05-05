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
    ) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """INSERT INTO store_entities
                (id, owner_user_id, owner_username, type, name, description,
                 category, version, photo_path, video_url, doc_paths,
                 file_size, install_count, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)""",
            [
                id, owner_user_id, owner_username, type, name, description,
                category, version, photo_path, video_url,
                json.dumps(doc_paths or []),
                int(file_size), now, now,
            ],
        )
        return self.get(id)  # type: ignore[return-value]

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
        self, owner_user_id: str, name: str
    ) -> Optional[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM store_entities WHERE owner_user_id = ? AND name = ?",
            [owner_user_id, name],
        ).fetchall()
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
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Filtered + paginated listing.

        Returns ``(items, total)`` where ``total`` is the unfiltered count
        across pages — used to render pagination controls.
        """
        clauses: List[str] = []
        params: List[Any] = []
        if type:
            clauses.append("type = ?"); params.append(type)
        if category:
            clauses.append("category = ?"); params.append(category)
        if owner_user_id:
            clauses.append("owner_user_id = ?"); params.append(owner_user_id)
        if search:
            clauses.append("(LOWER(name) LIKE ? OR LOWER(description) LIKE ?)")
            like = f"%{search.lower()}%"
            params.extend([like, like])
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
