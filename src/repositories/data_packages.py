"""Repository for ``data_packages`` + ``data_package_tables`` (v49).

A Data Package is an admin-curated bundle of tables (M:N to ``table_registry``)
that serves as the unit of "Add to stack" on /catalog. Seeded inline from the
``/admin/tables`` typeahead per Section 7 of the unified-stack design doc.

The FK on ``data_package_tables.package_id REFERENCES data_packages(id)`` does
not declare ``ON DELETE CASCADE`` (DuckDB constraint surface is narrower than
Postgres). ``delete()`` clears the junction explicitly so callers don't have
to remember the order.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from uuid import uuid4

import duckdb


class DataPackagesRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    # -- CRUD --------------------------------------------------------------

    # v51: column list — kept as a single constant so the SELECT in every
    # method stays in sync after future schema additions. Touch this when
    # adding columns.
    _COLS = [
        "id", "slug", "name", "description", "icon", "color",
        "cover_image_url", "status", "category",
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
        category: Optional[str] = None,
    ) -> str:
        """Insert a new package; returns the generated id.

        Raises ``duckdb.ConstraintException`` if ``slug`` collides — the
        UNIQUE constraint on ``data_packages.slug`` is the source of truth
        (no pre-check race window).
        """
        pkg_id = "pkg_" + uuid4().hex[:12]
        self.conn.execute(
            "INSERT INTO data_packages"
            "(id, slug, name, description, icon, color, cover_image_url, "
            " status, category, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [pkg_id, slug, name, description, icon, color, cover_image_url,
             status or "prod", category, created_by],
        )
        return pkg_id

    def get(self, pkg_id: str, *, include_deleted: bool = False) -> Optional[Dict[str, Any]]:
        # v54: soft-deleted rows are hidden by default. include_deleted=True
        # is the escape hatch the /restore endpoint uses to find them.
        guard = "" if include_deleted else " AND deleted_at IS NULL"
        row = self.conn.execute(
            f"SELECT {self._SELECT} FROM data_packages WHERE id = ?{guard}",
            [pkg_id],
        ).fetchone()
        if not row:
            return None
        return dict(zip(self._COLS, row))

    def get_by_slug(self, slug: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT id FROM data_packages WHERE slug = ?", [slug]
        ).fetchone()
        return self.get(row[0]) if row else None

    def list(
        self,
        *,
        search: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        # v54: filter soft-deleted rows; combined with optional search.
        query = f"SELECT {self._SELECT} FROM data_packages WHERE deleted_at IS NULL"
        params: List[Any] = []
        if search:
            query += " AND name ILIKE ?"
            params.append(f"%{search}%")
        query += " ORDER BY name LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(query, params).fetchall()
        return [dict(zip(self._COLS, r)) for r in rows]

    def update(
        self,
        pkg_id: str,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        icon: Optional[str] = None,
        color: Optional[str] = None,
        cover_image_url: Optional[str] = None,
        clear_cover_image: bool = False,
        status: Optional[str] = None,
        category: Optional[str] = None,
        clear_category: bool = False,
    ) -> None:
        """Partial update. ``cover_image_url`` follows the same Optional-is-no-op
        contract as the rest; pass ``clear_cover_image=True`` to actively NULL
        the column (admin removed the uploaded image). ``clear_category`` is
        the same NULL-clearing escape hatch for the v51 category field."""
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
        if clear_category:
            fields.append("category = NULL")
        elif category is not None:
            fields.append("category = ?")
            params.append(category)
        if not fields:
            return
        fields.append("updated_at = current_timestamp")
        params.append(pkg_id)
        self.conn.execute(
            f"UPDATE data_packages SET {', '.join(fields)} WHERE id = ?",
            params,
        )

    def delete(self, pkg_id: str) -> None:
        """v54: soft delete — sets ``deleted_at`` to now. The junction
        (``data_package_tables``) and any ``resource_grants`` referencing
        this package are intentionally preserved so the undo flow can
        restore the package whole. ``list()`` / ``get()`` filter
        ``deleted_at IS NULL`` so soft-deleted rows are invisible to
        every caller except the explicit ``restore`` path.

        Hard-delete (with junction cascade) is available via
        ``hard_delete`` — keep it for future admin cleanup workflows.
        """
        self.conn.execute(
            "UPDATE data_packages SET deleted_at = current_timestamp WHERE id = ?",
            [pkg_id],
        )

    def restore(self, pkg_id: str) -> None:
        """Reverse a soft delete. No-op if the row isn't currently
        soft-deleted (idempotent — guards against double-undo)."""
        self.conn.execute(
            "UPDATE data_packages SET deleted_at = NULL WHERE id = ?",
            [pkg_id],
        )

    def hard_delete(self, pkg_id: str) -> None:
        """Permanent delete — wipes the row + junction. Use when an admin
        wants the resource gone for good. Not currently wired into any
        endpoint; kept for completeness."""
        self.conn.execute(
            "DELETE FROM data_package_tables WHERE package_id = ?", [pkg_id]
        )
        self.conn.execute("DELETE FROM data_packages WHERE id = ?", [pkg_id])

    # -- Junction (package ↔ tables) ---------------------------------------

    def add_table(self, pkg_id: str, table_id: str, *, added_by: str) -> bool:
        """Insert a junction row. Returns True iff a new row was inserted."""
        before = self.conn.execute(
            "SELECT 1 FROM data_package_tables WHERE package_id = ? AND table_id = ?",
            [pkg_id, table_id],
        ).fetchone()
        if before:
            return False
        self.conn.execute(
            "INSERT INTO data_package_tables(package_id, table_id, added_by) "
            "VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
            [pkg_id, table_id, added_by],
        )
        return True

    def remove_table(self, pkg_id: str, table_id: str) -> bool:
        """Drop a junction row. Returns True iff a row was deleted."""
        before = self.conn.execute(
            "SELECT 1 FROM data_package_tables WHERE package_id = ? AND table_id = ?",
            [pkg_id, table_id],
        ).fetchone()
        if not before:
            return False
        self.conn.execute(
            "DELETE FROM data_package_tables WHERE package_id = ? AND table_id = ?",
            [pkg_id, table_id],
        )
        return True

    def list_tables(self, pkg_id: str) -> List[Dict[str, Any]]:
        """Tables belonging to a package (name-ordered)."""
        rows = self.conn.execute(
            "SELECT tr.id, tr.name "
            "FROM data_package_tables dpt "
            "JOIN table_registry tr ON tr.id = dpt.table_id "
            "WHERE dpt.package_id = ? ORDER BY tr.name",
            [pkg_id],
        ).fetchall()
        return [{"id": r[0], "name": r[1]} for r in rows]

    def list_packages_of_table(self, table_id: str) -> List[Dict[str, Any]]:
        """Packages a given table belongs to (name-ordered)."""
        rows = self.conn.execute(
            "SELECT dp.id, dp.slug, dp.name, dp.description, dp.icon, dp.color, "
            "       dp.cover_image_url "
            "FROM data_package_tables dpt "
            "JOIN data_packages dp ON dp.id = dpt.package_id "
            "WHERE dpt.table_id = ? ORDER BY dp.name",
            [table_id],
        ).fetchall()
        return [
            {
                "id": r[0], "slug": r[1], "name": r[2],
                "description": r[3], "icon": r[4], "color": r[5],
                "cover_image_url": r[6],
            }
            for r in rows
        ]
