"""Postgres-backed repository for ``memory_domains`` + ``knowledge_item_domains``.

Mirrors ``src/repositories/memory_domains.py`` (the DuckDB impl) on the
``MemoryDomainsRepository`` public surface. Cross-engine parity will be
covered by ``tests/db_pg/test_memory_domains_contract.py`` (Task 1D.2).

Implementation differences vs. DuckDB:

- No JSON list columns on this entity — no ``CAST(:p AS JSONB)`` helper
  needed (the data_packages_pg pattern doesn't apply here).
- ``add_item`` / ``remove_item`` use ``rowcount`` rather than a pre-SELECT
  race window: ``INSERT ... ON CONFLICT (item_id, domain_id) DO NOTHING``
  for add, plain DELETE for remove.
- ``knowledge_item_domains.domain_id`` carries ``ON DELETE CASCADE`` on
  the PG side (Task 1A.3 decision); ``hard_delete`` keeps the explicit
  junction wipe as belt-and-braces parity with the DuckDB sibling.

Schema drift note: the DuckDB ``knowledge_items`` table has an
``is_required`` column (v55) that the PG model does NOT replicate (Task
1A.3 scope was the v49+ Memory Domains entities, not the v55 column on
the pre-existing knowledge_items table). The DuckDB sibling's
``list_items_of_domain`` projects ``ki.is_required`` and ``ki.content``;
the PG mirror surfaces a literal ``False`` for ``is_required`` so the
return-shape stays parity-compatible. Closing this drift is plan-Task
followup — see commit body.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.engine import Engine


class MemoryDomainsPgRepository:
    """Postgres twin of ``MemoryDomainsRepository``."""

    def __init__(self, engine: Engine):
        self._engine = engine

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

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

        Raises an ``IntegrityError`` if ``slug`` collides — the UNIQUE
        constraint on ``memory_domains.slug`` is the source of truth.
        """
        domain_id = "md_" + uuid4().hex[:12]
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """
                    INSERT INTO memory_domains
                      (id, slug, name, description, icon, color,
                       cover_image_url, status, created_by)
                    VALUES
                      (:id, :slug, :name, :description, :icon, :color,
                       :cover_image_url, :status, :created_by)
                    """
                ),
                {
                    "id": domain_id,
                    "slug": slug,
                    "name": name,
                    "description": description,
                    "icon": icon,
                    "color": color,
                    "cover_image_url": cover_image_url,
                    "status": status or "prod",
                    "created_by": created_by,
                },
            )
        return domain_id

    def ensure_seed(
        self,
        *,
        domain_id: str,
        slug: str,
        name: str,
        icon: Optional[str] = None,
        color: Optional[str] = None,
    ) -> bool:
        """Idempotently insert a canonical domain under its deterministic id.

        Mirrors the DuckDB sibling: never modifies an existing row — a
        matching id or slug (including a soft-deleted row, which still
        holds its slug) makes this a no-op via ``ON CONFLICT DO NOTHING``,
        so admin edits and deletions survive reboots. Returns True iff a
        new row was inserted.
        """
        with self._engine.begin() as conn:
            res = conn.execute(
                sa.text(
                    """
                    INSERT INTO memory_domains
                      (id, slug, name, icon, color, status, created_by)
                    VALUES
                      (:id, :slug, :name, :icon, :color, 'prod', 'system:seed')
                    ON CONFLICT DO NOTHING
                    """
                ),
                {
                    "id": domain_id,
                    "slug": slug,
                    "name": name,
                    "icon": icon,
                    "color": color,
                },
            )
        return bool(res.rowcount)

    def get(
        self, domain_id: str, *, include_deleted: bool = False
    ) -> Optional[Dict[str, Any]]:
        """Fetch a single domain. Soft-deleted rows are hidden by default
        — pass ``include_deleted=True`` (used by /restore)."""
        guard = "" if include_deleted else " AND deleted_at IS NULL"
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    f"SELECT * FROM memory_domains WHERE id = :id{guard}"
                ),
                {"id": domain_id},
            ).mappings().first()
        return dict(row) if row else None

    def get_by_slug(self, slug: str) -> Optional[Dict[str, Any]]:
        """Slug → row lookup. Filters soft-deleted rows so a recreate
        with the same slug doesn't resurrect the wrong row."""
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT id FROM memory_domains "
                    "WHERE slug = :slug AND deleted_at IS NULL"
                ),
                {"slug": slug},
            ).first()
        return self.get(row[0]) if row else None

    def exists_by_slug(self, slug: str) -> bool:
        """Cheap predicate for slug validation in the API layer.

        Replaces the pre-v49 hardcoded ``VALID_DOMAINS`` list check.
        """
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT 1 FROM memory_domains "
                    "WHERE slug = :slug AND deleted_at IS NULL"
                ),
                {"slug": slug},
            ).first()
        return row is not None

    def list(
        self,
        *,
        search: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """List live domains, name-ordered, with optional name search."""
        query = "SELECT * FROM memory_domains WHERE deleted_at IS NULL"
        params: Dict[str, Any] = {}
        if search:
            query += " AND name ILIKE :search"
            params["search"] = f"%{search}%"
        query += " ORDER BY name LIMIT :limit"
        params["limit"] = limit
        with self._engine.connect() as conn:
            rows = conn.execute(sa.text(query), params).mappings().all()
        return [dict(r) for r in rows]

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
        """Partial update. Matches the DuckDB sibling's Optional-is-no-op
        contract; ``clear_cover_image`` is the explicit NULL-clearing
        escape hatch for ``cover_image_url``."""
        plain_candidates = {
            "name": name,
            "description": description,
            "icon": icon,
            "color": color,
            "status": status,
        }

        fields: List[str] = []
        params: Dict[str, Any] = {}

        for col, val in plain_candidates.items():
            if val is not None:
                fields.append(f"{col} = :{col}")
                params[col] = val

        if clear_cover_image:
            fields.append("cover_image_url = NULL")
        elif cover_image_url is not None:
            fields.append("cover_image_url = :cover_image_url")
            params["cover_image_url"] = cover_image_url

        if not fields:
            return

        fields.append("updated_at = CURRENT_TIMESTAMP")
        params["domain_id"] = domain_id
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    f"UPDATE memory_domains SET {', '.join(fields)} "
                    f"WHERE id = :domain_id"
                ),
                params,
            )

    def delete(self, domain_id: str) -> None:
        """Soft-delete: sets ``deleted_at`` to now. Junction rows and any
        ``resource_grants`` referencing this domain are preserved so the
        undo flow can restore the domain whole."""
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE memory_domains SET deleted_at = CURRENT_TIMESTAMP "
                    "WHERE id = :id"
                ),
                {"id": domain_id},
            )

    def restore(self, domain_id: str) -> None:
        """Reverse a soft delete. Idempotent."""
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE memory_domains SET deleted_at = NULL "
                    "WHERE id = :id"
                ),
                {"id": domain_id},
            )

    def hard_delete(self, domain_id: str) -> None:
        """Permanent delete — wipes the row + junction. The PG schema has
        ``ON DELETE CASCADE`` on ``knowledge_item_domains.domain_id`` so
        the explicit junction clear is belt-and-braces parity with the
        DuckDB sibling."""
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "DELETE FROM knowledge_item_domains "
                    "WHERE domain_id = :id"
                ),
                {"id": domain_id},
            )
            conn.execute(
                sa.text("DELETE FROM memory_domains WHERE id = :id"),
                {"id": domain_id},
            )

    def resolve_ids_to_slugs(self, domain_ids: List[str]) -> Dict[str, str]:
        """Resolve a batch of domain ids to their slugs (live rows only).

        Returns ``{id: slug}`` for every input id that maps to a live
        (non-soft-deleted) domain. Empty input → ``{}``; unknown and
        soft-deleted ids are silently omitted. Mirrors the DuckDB sibling;
        carries the ``deleted_at IS NULL`` guard the original inline SQL
        in ``app/api/memory.py`` admin_patch_item lacked.
        """
        if not domain_ids:
            return {}
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    "SELECT id, slug FROM memory_domains "
                    "WHERE id = ANY(:ids) AND deleted_at IS NULL"
                ),
                {"ids": list(domain_ids)},
            ).all()
        return {r[0]: r[1] for r in rows}

    # ------------------------------------------------------------------
    # Junction (domain ↔ items)
    # ------------------------------------------------------------------

    def add_item(self, domain_id: str, item_id: str, *, added_by: str) -> bool:
        """Insert a junction row. Returns True iff a new row was inserted."""
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.text(
                    """
                    INSERT INTO knowledge_item_domains
                      (domain_id, item_id, added_by)
                    VALUES (:domain_id, :item_id, :added_by)
                    ON CONFLICT (item_id, domain_id) DO NOTHING
                    """
                ),
                {
                    "domain_id": domain_id,
                    "item_id": item_id,
                    "added_by": added_by,
                },
            )
            return (result.rowcount or 0) > 0

    def remove_item(self, domain_id: str, item_id: str) -> bool:
        """Drop a junction row. Returns True iff a row was deleted."""
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.text(
                    "DELETE FROM knowledge_item_domains "
                    "WHERE domain_id = :domain_id AND item_id = :item_id"
                ),
                {"domain_id": domain_id, "item_id": item_id},
            )
            return (result.rowcount or 0) > 0

    def list_items_of_domain(
        self, domain_id: str, *, limit: int = 1000
    ) -> List[Dict[str, Any]]:
        """Items tagged with a given domain (title-ordered).

        Projects ``id, title, status, is_required, content`` for parity
        with the DuckDB sibling. ``knowledge_items.is_required`` does
        NOT exist in the PG schema yet (see module docstring); surfaces
        as a literal ``False`` so the return shape matches.
        """
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    """
                    SELECT ki.id, ki.title, ki.status,
                           FALSE AS is_required,
                           ki.content
                    FROM knowledge_item_domains kid
                    JOIN knowledge_items ki ON ki.id = kid.item_id
                    WHERE kid.domain_id = :domain_id
                    ORDER BY ki.title
                    LIMIT :limit
                    """
                ),
                {"domain_id": domain_id, "limit": limit},
            ).mappings().all()
        return [
            {
                "id": r["id"],
                "title": r["title"],
                "status": r["status"],
                "is_required": bool(r["is_required"]),
                "content": r["content"],
            }
            for r in rows
        ]

    def list_domains_of_item(self, item_id: str) -> List[Dict[str, Any]]:
        """Domains an item is tagged with (name-ordered).

        Mirrors the DuckDB sibling's projection: ``{id, slug, name, icon,
        color, cover_image_url}``. Filters soft-deleted domains.
        """
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    """
                    SELECT md.id, md.slug, md.name, md.icon, md.color,
                           md.cover_image_url
                    FROM knowledge_item_domains kid
                    JOIN memory_domains md ON md.id = kid.domain_id
                    WHERE kid.item_id = :item_id
                      AND md.deleted_at IS NULL
                    ORDER BY md.name
                    """
                ),
                {"item_id": item_id},
            ).mappings().all()
        return [dict(r) for r in rows]

    def replace_domains_for_item(
        self,
        item_id: str,
        slugs: List[str],
        *,
        added_by: str,
    ) -> List[str]:
        """Set the item's domain membership to exactly ``slugs``
        (delete-then-insert). Unknown slugs raise ``ValueError`` —
        admin must pre-create domains via the dedicated CRUD endpoint.
        Returns the resolved ``memory_domains.id`` list."""
        with self._engine.begin() as conn:
            if not slugs:
                conn.execute(
                    sa.text(
                        "DELETE FROM knowledge_item_domains "
                        "WHERE item_id = :item_id"
                    ),
                    {"item_id": item_id},
                )
                return []

            # Resolve all slugs first so we don't half-write on a typo.
            rows = conn.execute(
                sa.text(
                    "SELECT slug, id FROM memory_domains "
                    "WHERE slug = ANY(:slugs)"
                ),
                {"slugs": list(slugs)},
            ).all()
            resolved = {r[0]: r[1] for r in rows}
            missing = [s for s in slugs if s not in resolved]
            if missing:
                raise ValueError(
                    f"Unknown memory domain slug(s): {missing}"
                )

            conn.execute(
                sa.text(
                    "DELETE FROM knowledge_item_domains "
                    "WHERE item_id = :item_id"
                ),
                {"item_id": item_id},
            )
            for slug, did in resolved.items():
                conn.execute(
                    sa.text(
                        """
                        INSERT INTO knowledge_item_domains
                          (item_id, domain_id, added_by)
                        VALUES (:item_id, :domain_id, :added_by)
                        ON CONFLICT (item_id, domain_id) DO NOTHING
                        """
                    ),
                    {
                        "item_id": item_id,
                        "domain_id": did,
                        "added_by": added_by,
                    },
                )
            return list(resolved.values())
