"""Repository for ``memory_domains`` + ``knowledge_item_domains`` (v49).

Memory domains are first-class entities replacing the v15 scalar
``knowledge_items.domain`` string. An item can belong to multiple domains
through ``knowledge_item_domains``. Admin can create non-canonical domains
beyond the six legacy ``VALID_DOMAINS`` (finance, engineering, …) that the
v49 migration seeded with deterministic IDs (``md_<slug>``).

The repo's ``create`` uses ``md_<uuid>`` for admin-authored rows; the seed
IDs are stable so migrations + tests can rely on ``md_finance`` etc.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from uuid import uuid4

import duckdb


class MemoryDomainsRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    # -- CRUD --------------------------------------------------------------

    # v51: column list — keep in sync after schema additions. Mirrors the
    # DataPackagesRepository pattern.
    _COLS = [
        "id", "slug", "name", "description", "icon", "color",
        "cover_image_url", "status",
        "created_by", "created_at", "updated_at",
    ]
    _SELECT = ", ".join(_COLS)

    def create(
        self,
        *,
        name: str,
        slug: str,
        description: Optional[str],
        icon: Optional[str],
        color: Optional[str],
        created_by: str,
        cover_image_url: Optional[str] = None,
        status: str = "prod",
    ) -> str:
        """Insert a new domain; returns the generated id (``md_<uuid12>``).

        Raises ``duckdb.ConstraintException`` if ``slug`` collides — UNIQUE
        on the column is the source of truth.
        """
        domain_id = "md_" + uuid4().hex[:12]
        self.conn.execute(
            "INSERT INTO memory_domains"
            "(id, slug, name, description, icon, color, cover_image_url, "
            " status, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [domain_id, slug, name, description, icon, color,
             cover_image_url, status or "prod", created_by],
        )
        return domain_id

    def get(self, domain_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            f"SELECT {self._SELECT} FROM memory_domains WHERE id = ?",
            [domain_id],
        ).fetchone()
        if not row:
            return None
        return dict(zip(self._COLS, row))

    def get_by_slug(self, slug: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT id FROM memory_domains WHERE slug = ?", [slug]
        ).fetchone()
        return self.get(row[0]) if row else None

    def exists_by_slug(self, slug: str) -> bool:
        """Cheap predicate for slug validation in the API layer.

        Replaces the pre-v49 ``if domain not in VALID_DOMAINS`` hardcoded
        list check in ``app/api/memory.py``.
        """
        row = self.conn.execute(
            "SELECT 1 FROM memory_domains WHERE slug = ?", [slug]
        ).fetchone()
        return row is not None

    def list(
        self,
        *,
        search: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        query = f"SELECT {self._SELECT} FROM memory_domains"
        params: List[Any] = []
        if search:
            query += " WHERE name ILIKE ?"
            params.append(f"%{search}%")
        query += " ORDER BY name LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(query, params).fetchall()
        return [dict(zip(self._COLS, r)) for r in rows]

    def update(
        self,
        domain_id: str,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        icon: Optional[str] = None,
        color: Optional[str] = None,
        cover_image_url: Optional[str] = None,
        clear_cover_image: bool = False,
        status: Optional[str] = None,
    ) -> None:
        """Partial update. ``cover_image_url`` follows the same Optional-is-no-op
        contract as the rest; pass ``clear_cover_image=True`` to actively NULL
        the column."""
        fields: List[str] = []
        params: List[Any] = []
        if name is not None:
            fields.append("name = ?")
            params.append(name)
        if description is not None:
            fields.append("description = ?")
            params.append(description)
        if icon is not None:
            fields.append("icon = ?")
            params.append(icon)
        if color is not None:
            fields.append("color = ?")
            params.append(color)
        if clear_cover_image:
            fields.append("cover_image_url = NULL")
        elif cover_image_url is not None:
            fields.append("cover_image_url = ?")
            params.append(cover_image_url)
        if status is not None:
            fields.append("status = ?")
            params.append(status)
        if not fields:
            return
        fields.append("updated_at = current_timestamp")
        params.append(domain_id)
        self.conn.execute(
            f"UPDATE memory_domains SET {', '.join(fields)} WHERE id = ?",
            params,
        )

    def delete(self, domain_id: str) -> None:
        """Drop the domain and its junction rows.

        DuckDB doesn't honor ON DELETE CASCADE on this FK declaration;
        clear the junction first so the order doesn't matter for callers.
        """
        self.conn.execute(
            "DELETE FROM knowledge_item_domains WHERE domain_id = ?", [domain_id]
        )
        self.conn.execute("DELETE FROM memory_domains WHERE id = ?", [domain_id])

    # -- Junction (domain ↔ items) -----------------------------------------

    def add_item(self, domain_id: str, item_id: str, *, added_by: str) -> bool:
        """Insert a junction row. Returns True iff a new row was inserted."""
        before = self.conn.execute(
            "SELECT 1 FROM knowledge_item_domains WHERE domain_id = ? AND item_id = ?",
            [domain_id, item_id],
        ).fetchone()
        if before:
            return False
        self.conn.execute(
            "INSERT INTO knowledge_item_domains(domain_id, item_id, added_by) "
            "VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
            [domain_id, item_id, added_by],
        )
        return True

    def remove_item(self, domain_id: str, item_id: str) -> bool:
        """Drop a junction row. Returns True iff a row was deleted."""
        before = self.conn.execute(
            "SELECT 1 FROM knowledge_item_domains WHERE domain_id = ? AND item_id = ?",
            [domain_id, item_id],
        ).fetchone()
        if not before:
            return False
        self.conn.execute(
            "DELETE FROM knowledge_item_domains WHERE domain_id = ? AND item_id = ?",
            [domain_id, item_id],
        )
        return True

    def list_items_of_domain(
        self, domain_id: str, *, limit: int = 1000
    ) -> List[Dict[str, Any]]:
        """Items tagged with a given domain (title-ordered)."""
        rows = self.conn.execute(
            "SELECT ki.id, ki.title, ki.status "
            "FROM knowledge_item_domains kid "
            "JOIN knowledge_items ki ON ki.id = kid.item_id "
            "WHERE kid.domain_id = ? "
            "ORDER BY ki.title LIMIT ?",
            [domain_id, limit],
        ).fetchall()
        return [{"id": r[0], "title": r[1], "status": r[2]} for r in rows]

    def list_domains_of_item(self, item_id: str) -> List[Dict[str, Any]]:
        """Domains an item is tagged with (name-ordered)."""
        rows = self.conn.execute(
            "SELECT md.id, md.slug, md.name, md.icon, md.color, md.cover_image_url "
            "FROM knowledge_item_domains kid "
            "JOIN memory_domains md ON md.id = kid.domain_id "
            "WHERE kid.item_id = ? ORDER BY md.name",
            [item_id],
        ).fetchall()
        return [
            {"id": r[0], "slug": r[1], "name": r[2], "icon": r[3], "color": r[4],
             "cover_image_url": r[5]}
            for r in rows
        ]

    def replace_domains_for_item(
        self,
        item_id: str,
        slugs: List[str],
        *,
        added_by: str,
    ) -> List[str]:
        """Set the item's domain membership to exactly ``slugs`` (delete-then-insert).

        Used by ``create_item`` / ``update_item`` paths in the API layer when
        the request carries a ``domain`` field. Unknown slugs raise
        ``ValueError`` — admin must pre-create domains via the dedicated CRUD
        endpoint. Returns the resolved ``memory_domains.id`` set written.
        """
        # Resolve all slugs first so we don't half-write on a typo.
        if not slugs:
            self.conn.execute(
                "DELETE FROM knowledge_item_domains WHERE item_id = ?",
                [item_id],
            )
            return []
        placeholders = ",".join(["?"] * len(slugs))
        rows = self.conn.execute(
            f"SELECT slug, id FROM memory_domains WHERE slug IN ({placeholders})",
            list(slugs),
        ).fetchall()
        resolved = {r[0]: r[1] for r in rows}
        missing = [s for s in slugs if s not in resolved]
        if missing:
            raise ValueError(f"Unknown memory domain slug(s): {missing}")
        self.conn.execute(
            "DELETE FROM knowledge_item_domains WHERE item_id = ?", [item_id]
        )
        for slug, did in resolved.items():
            self.conn.execute(
                "INSERT INTO knowledge_item_domains(item_id, domain_id, added_by) "
                "VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
                [item_id, did, added_by],
            )
        return list(resolved.values())
