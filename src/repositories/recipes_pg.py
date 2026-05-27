"""Postgres-backed repository for ``recipes``.

Mirrors ``src/repositories/recipes.py`` (the DuckDB impl) on the
``RecipesRepository`` public surface. Cross-engine parity covered by
``tests/db_pg/test_recipes_contract.py`` (Task 1D.4).

Implementation differences vs. DuckDB:

- ``related_table_ids`` is stored as JSONB. Writes go through
  ``CAST(:p AS JSONB)`` with a ``json.dumps(value)`` bind; reads come
  back as a native Python list via psycopg's adapter — no manual
  ``json.loads`` round-trip on the read path (unlike the DuckDB sibling
  which json-encodes VARCHAR and decodes on every fetch).
- No junction table on this entity; ``related_table_ids`` lives inline
  on the recipe row (per ``src/repositories/recipes.py`` module
  docstring — one-way relationship, small cardinality, junction would
  be more plumbing than it's worth).
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.engine import Engine


# Subset of writable columns that carry a JSON list. Used by ``create`` /
# ``update`` to know which bind params need a ``CAST(:p AS JSONB)`` wrap.
_JSON_COLUMNS = {"related_table_ids"}


def _json_param(v: Optional[List[Any]]) -> Optional[str]:
    """Serialize list to JSON text for the ``CAST(:p AS JSONB)`` bind.

    ``None`` passes through unchanged so the DB stores SQL NULL, not the
    JSON ``null`` literal.
    """
    if v is None:
        return None
    return json.dumps(v)


class RecipesPgRepository:
    """Postgres twin of ``RecipesRepository``."""

    def __init__(self, engine: Engine):
        self._engine = engine

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create(
        self,
        *,
        slug: str,
        title: str,
        description: Optional[str],
        icon: Optional[str],
        color: Optional[str],
        sql_template: Optional[str],
        related_table_ids: Optional[List[str]],
        status: str = "prod",
        created_by: Optional[str] = None,
    ) -> str:
        """Insert a new recipe; returns the generated id (``rcp_<uuid12>``).

        Raises an ``IntegrityError`` if ``slug`` collides — the UNIQUE
        constraint on ``recipes.slug`` is the source of truth.
        """
        recipe_id = "rcp_" + uuid4().hex[:12]
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """
                    INSERT INTO recipes
                      (id, slug, title, description, icon, color,
                       sql_template, related_table_ids, status, created_by)
                    VALUES
                      (:id, :slug, :title, :description, :icon, :color,
                       :sql_template, CAST(:related_table_ids AS JSONB),
                       :status, :created_by)
                    """
                ),
                {
                    "id": recipe_id,
                    "slug": slug,
                    "title": title,
                    "description": description,
                    "icon": icon,
                    "color": color,
                    "sql_template": sql_template,
                    "related_table_ids": _json_param(related_table_ids),
                    "status": status or "prod",
                    "created_by": created_by,
                },
            )
        return recipe_id

    @staticmethod
    def _normalize_row(row: Dict[str, Any]) -> Dict[str, Any]:
        """Default ``related_table_ids`` to ``[]`` when NULL.

        psycopg returns JSONB as native Python lists already, so the only
        normalisation needed is the NULL-to-empty-list defaulting that
        mirrors the DuckDB sibling's ``_decode_row``.
        """
        for k in _JSON_COLUMNS:
            v = row.get(k)
            if v is None:
                row[k] = []
            elif isinstance(v, str):
                # Defensive: handle legacy string-encoded rows (none
                # exist in PG today, but cheap to be safe).
                try:
                    parsed = json.loads(v)
                    row[k] = parsed if isinstance(parsed, list) else []
                except (ValueError, TypeError):
                    row[k] = []
            elif not isinstance(v, list):
                row[k] = []
        return row

    def get(
        self, recipe_id: str, *, include_deleted: bool = False
    ) -> Optional[Dict[str, Any]]:
        """Fetch a single recipe. Soft-deleted rows are hidden by default
        — pass ``include_deleted=True`` (used by /restore)."""
        guard = "" if include_deleted else " AND deleted_at IS NULL"
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    f"SELECT * FROM recipes WHERE id = :id{guard}"
                ),
                {"id": recipe_id},
            ).mappings().first()
        return self._normalize_row(dict(row)) if row else None

    def get_by_slug(self, slug: str) -> Optional[Dict[str, Any]]:
        """Slug → row lookup. Filters soft-deleted rows so a recreate
        with the same slug doesn't resurrect the wrong row."""
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT id FROM recipes "
                    "WHERE slug = :slug AND deleted_at IS NULL"
                ),
                {"slug": slug},
            ).first()
        return self.get(row[0]) if row else None

    def list(
        self,
        *,
        search: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """List live recipes, title-ordered, with optional title search."""
        query = "SELECT * FROM recipes WHERE deleted_at IS NULL"
        params: Dict[str, Any] = {}
        if search:
            query += " AND title ILIKE :search"
            params["search"] = f"%{search}%"
        query += " ORDER BY title LIMIT :limit"
        params["limit"] = limit
        with self._engine.connect() as conn:
            rows = conn.execute(sa.text(query), params).mappings().all()
        return [self._normalize_row(dict(r)) for r in rows]

    def update(
        self,
        recipe_id: str,
        *,
        title: Optional[str] = None,
        description: Optional[str] = None,
        icon: Optional[str] = None,
        color: Optional[str] = None,
        sql_template: Optional[str] = None,
        related_table_ids: Optional[List[str]] = None,
        status: Optional[str] = None,
        clear_related_tables: bool = False,
    ) -> None:
        """Partial update. Matches the DuckDB sibling's Optional-is-no-op
        contract; ``clear_related_tables`` is the explicit NULL-clearing
        escape hatch for ``related_table_ids`` (the DuckDB sibling clears
        to SQL NULL; the read path normalises NULL → ``[]``)."""
        plain_candidates = {
            "title": title,
            "description": description,
            "icon": icon,
            "color": color,
            "sql_template": sql_template,
            "status": status,
        }

        fields: List[str] = []
        params: Dict[str, Any] = {}

        for col, val in plain_candidates.items():
            if val is not None:
                fields.append(f"{col} = :{col}")
                params[col] = val

        # related_table_ids: clear-flag takes precedence over value (mirrors
        # the DuckDB sibling), and writes go through CAST(:p AS JSONB).
        if clear_related_tables:
            fields.append("related_table_ids = NULL")
        elif related_table_ids is not None:
            fields.append("related_table_ids = CAST(:related_table_ids AS JSONB)")
            params["related_table_ids"] = json.dumps(related_table_ids)

        if not fields:
            return

        fields.append("updated_at = CURRENT_TIMESTAMP")
        params["recipe_id"] = recipe_id
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    f"UPDATE recipes SET {', '.join(fields)} "
                    f"WHERE id = :recipe_id"
                ),
                params,
            )

    def delete(self, recipe_id: str) -> None:
        """Soft-delete: sets ``deleted_at`` to now. Undo via ``restore``."""
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE recipes SET deleted_at = CURRENT_TIMESTAMP "
                    "WHERE id = :id"
                ),
                {"id": recipe_id},
            )

    def restore(self, recipe_id: str) -> None:
        """Reverse a soft delete. Idempotent."""
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE recipes SET deleted_at = NULL "
                    "WHERE id = :id"
                ),
                {"id": recipe_id},
            )

    def hard_delete(self, recipe_id: str) -> None:
        """Permanent delete. Not currently exposed via the API."""
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM recipes WHERE id = :id"),
                {"id": recipe_id},
            )
