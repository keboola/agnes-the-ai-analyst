"""Repository for ``recipes`` (v53).

A Recipe is an admin-curated, multi-table query template that analysts
copy + adapt. Sibling concept to Data Packages on /catalog — but recipes
aren't stack-subscribable. Analysts use a recipe; they don't opt in.

``related_table_ids`` lives as a JSON array on the row rather than a
junction table — the relationship is one-way (recipe → tables it touches)
and per-recipe cardinality is small, so the junction would be more SQL
plumbing than it's worth at this stage.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from uuid import uuid4

import duckdb


class RecipesRepository:
    _COLS = [
        "id", "slug", "title", "description", "icon", "color",
        "sql_template", "related_table_ids", "status",
        "created_by", "created_at", "updated_at",
    ]
    _SELECT = ", ".join(_COLS)

    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    @classmethod
    def _decode_row(cls, row) -> Dict[str, Any]:
        d = dict(zip(cls._COLS, row))
        v = d.get("related_table_ids")
        if v is None or v == "":
            d["related_table_ids"] = []
        elif isinstance(v, str):
            try:
                parsed = json.loads(v)
                d["related_table_ids"] = parsed if isinstance(parsed, list) else []
            except Exception:
                d["related_table_ids"] = []
        elif not isinstance(v, list):
            d["related_table_ids"] = []
        return d

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
        recipe_id = "rcp_" + uuid4().hex[:12]
        self.conn.execute(
            "INSERT INTO recipes "
            "(id, slug, title, description, icon, color, sql_template, "
            " related_table_ids, status, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                recipe_id, slug, title, description, icon, color, sql_template,
                json.dumps(related_table_ids or []),
                status or "prod",
                created_by,
            ],
        )
        return recipe_id

    def get(self, recipe_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            f"SELECT {self._SELECT} FROM recipes WHERE id = ?", [recipe_id]
        ).fetchone()
        return self._decode_row(row) if row else None

    def get_by_slug(self, slug: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            f"SELECT {self._SELECT} FROM recipes WHERE slug = ?", [slug]
        ).fetchone()
        return self._decode_row(row) if row else None

    def list(self, *, search: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        query = f"SELECT {self._SELECT} FROM recipes"
        params: List[Any] = []
        if search:
            query += " WHERE title ILIKE ?"
            params.append(f"%{search}%")
        query += " ORDER BY title LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(query, params).fetchall()
        return [self._decode_row(r) for r in rows]

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
        fields: List[str] = []
        params: List[Any] = []
        if title is not None:
            fields.append("title = ?"); params.append(title)
        if description is not None:
            fields.append("description = ?"); params.append(description)
        if icon is not None:
            fields.append("icon = ?"); params.append(icon)
        if color is not None:
            fields.append("color = ?"); params.append(color)
        if sql_template is not None:
            fields.append("sql_template = ?"); params.append(sql_template)
        if clear_related_tables:
            fields.append("related_table_ids = NULL")
        elif related_table_ids is not None:
            fields.append("related_table_ids = ?")
            params.append(json.dumps(related_table_ids))
        if status is not None:
            fields.append("status = ?"); params.append(status)
        if not fields:
            return
        fields.append("updated_at = current_timestamp")
        params.append(recipe_id)
        self.conn.execute(
            f"UPDATE recipes SET {', '.join(fields)} WHERE id = ?",
            params,
        )

    def delete(self, recipe_id: str) -> None:
        self.conn.execute("DELETE FROM recipes WHERE id = ?", [recipe_id])
