"""Postgres-backed metric_definitions repository.

Mirrors ``src/repositories/metrics.py``. The DuckDB ``list_contains(arr, x)``
becomes PG's ``x = ANY(arr)`` operator; ``unnest()`` stays the same.

``import_from_yaml`` from the DuckDB original is not ported here — that
helper is a backend-agnostic I/O wrapper around ``create()`` and belongs in
a shared file outside the repository class. Callers should construct rows
and call ``create()`` directly until the helper is moved.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine


def _json_dumps(obj) -> Optional[str]:
    return None if obj is None else json.dumps(obj)


class MetricPgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    def create(
        self,
        id: str,
        name: str,
        display_name: str,
        category: str,
        sql: str,
        description: Optional[str] = None,
        type: str = "sum",
        unit: Optional[str] = None,
        grain: str = "monthly",
        table_name: Optional[str] = None,
        tables: Optional[List[str]] = None,
        expression: Optional[str] = None,
        time_column: Optional[str] = None,
        dimensions: Optional[List[str]] = None,
        filters: Optional[List[str]] = None,
        synonyms: Optional[List[str]] = None,
        notes: Optional[List[str]] = None,
        sql_variants: Optional[Dict[str, Any]] = None,
        validation: Optional[Dict[str, Any]] = None,
        source: str = "manual",
        **kwargs,
    ) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """INSERT INTO metric_definitions (
                        id, name, display_name, category, description, type, unit, grain,
                        table_name, tables, expression, time_column, dimensions, filters,
                        synonyms, notes, sql, sql_variants, validation, source,
                        created_at, updated_at
                    ) VALUES (
                        :id, :name, :display_name, :category, :description, :type, :unit, :grain,
                        :table_name, :tables, :expression, :time_column, :dimensions, :filters,
                        :synonyms, :notes, :sql,
                        CAST(:sql_variants AS JSONB), CAST(:validation AS JSONB), :source,
                        :now, :now
                    )
                    ON CONFLICT (id) DO UPDATE SET
                        name = EXCLUDED.name,
                        display_name = EXCLUDED.display_name,
                        category = EXCLUDED.category,
                        description = EXCLUDED.description,
                        type = EXCLUDED.type,
                        unit = EXCLUDED.unit,
                        grain = EXCLUDED.grain,
                        table_name = EXCLUDED.table_name,
                        tables = EXCLUDED.tables,
                        expression = EXCLUDED.expression,
                        time_column = EXCLUDED.time_column,
                        dimensions = EXCLUDED.dimensions,
                        filters = EXCLUDED.filters,
                        synonyms = EXCLUDED.synonyms,
                        notes = EXCLUDED.notes,
                        sql = EXCLUDED.sql,
                        sql_variants = EXCLUDED.sql_variants,
                        validation = EXCLUDED.validation,
                        source = EXCLUDED.source,
                        updated_at = EXCLUDED.updated_at"""
                ),
                {
                    "id": id, "name": name, "display_name": display_name,
                    "category": category, "description": description, "type": type,
                    "unit": unit, "grain": grain, "table_name": table_name,
                    "tables": tables, "expression": expression, "time_column": time_column,
                    "dimensions": dimensions, "filters": filters,
                    "synonyms": synonyms, "notes": notes, "sql": sql,
                    "sql_variants": _json_dumps(sql_variants),
                    "validation": _json_dumps(validation),
                    "source": source, "now": now,
                },
            )
        return self.get(id)  # type: ignore[return-value]

    def get(self, metric_id: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT * FROM metric_definitions WHERE id = :id"),
                {"id": metric_id},
            ).mappings().first()
        return dict(row) if row else None

    def list(self, category: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            if category is not None:
                rows = conn.execute(
                    sa.text(
                        "SELECT * FROM metric_definitions WHERE category = :c ORDER BY name"
                    ),
                    {"c": category},
                ).mappings().all()
            else:
                rows = conn.execute(
                    sa.text("SELECT * FROM metric_definitions ORDER BY name")
                ).mappings().all()
        return [dict(r) for r in rows]

    def update(self, metric_id: str, **kwargs) -> Optional[Dict[str, Any]]:
        existing = self.get(metric_id)
        if existing is None:
            return None
        allowed = {
            "name", "display_name", "category", "description", "type", "unit",
            "grain", "table_name", "tables", "expression", "time_column",
            "dimensions", "filters", "synonyms", "notes", "sql",
            "sql_variants", "validation", "source",
        }
        json_fields = {"sql_variants", "validation"}
        updates = {}
        for k, v in kwargs.items():
            if k in allowed:
                updates[k] = _json_dumps(v) if k in json_fields else v
        if not updates:
            return existing
        updates["updated_at"] = datetime.now(timezone.utc)
        set_clause = ", ".join(
            f"{k} = CAST(:{k} AS JSONB)" if k in json_fields else f"{k} = :{k}"
            for k in updates
        )
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(f"UPDATE metric_definitions SET {set_clause} WHERE id = :metric_id"),
                {**updates, "metric_id": metric_id},
            )
        return self.get(metric_id)

    def delete(self, metric_id: str) -> bool:
        existing = self.get(metric_id)
        if existing is None:
            return False
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM metric_definitions WHERE id = :id"),
                {"id": metric_id},
            )
        return True

    def find_by_table(self, table_name: str) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    """SELECT * FROM metric_definitions
                       WHERE table_name = :tname OR :tname = ANY(tables)
                       ORDER BY name"""
                ),
                {"tname": table_name},
            ).mappings().all()
        return [dict(r) for r in rows]

    def find_by_synonym(self, term: str) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    "SELECT * FROM metric_definitions WHERE :term = ANY(synonyms) ORDER BY name"
                ),
                {"term": term},
            ).mappings().all()
        return [dict(r) for r in rows]

    def get_table_map(self) -> Dict[str, List[str]]:
        result: Dict[str, List[str]] = {}
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    "SELECT table_name, name FROM metric_definitions "
                    "WHERE table_name IS NOT NULL ORDER BY table_name, name"
                )
            ).all()
            for tn, mn in rows:
                result.setdefault(tn, []).append(mn)
            rows2 = conn.execute(
                sa.text(
                    "SELECT unnest(tables) AS tbl, name FROM metric_definitions "
                    "WHERE tables IS NOT NULL"
                )
            ).all()
            for tbl, mn in rows2:
                result.setdefault(tbl, []).append(mn)
        return result
