"""Repository for metric definitions."""

import json
from datetime import datetime, timezone
from typing import Any, Optional, List, Dict

import duckdb


def json_dumps(obj) -> Optional[str]:
    """Serialize obj to JSON string, or None if obj is None."""
    if obj is None:
        return None
    return json.dumps(obj)


class MetricRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def _row_to_dict(self, row) -> Optional[Dict[str, Any]]:
        if not row:
            return None
        columns = [desc[0] for desc in self.conn.description]
        return dict(zip(columns, row))

    def _rows_to_dicts(self, rows) -> List[Dict[str, Any]]:
        if not rows:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, row)) for row in rows]

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
        self.conn.execute(
            """INSERT INTO metric_definitions (
                id, name, display_name, category, description, type, unit, grain,
                table_name, tables, expression, time_column, dimensions, filters,
                synonyms, notes, sql, sql_variants, validation, source,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET
                name = excluded.name,
                display_name = excluded.display_name,
                category = excluded.category,
                description = excluded.description,
                type = excluded.type,
                unit = excluded.unit,
                grain = excluded.grain,
                table_name = excluded.table_name,
                tables = excluded.tables,
                expression = excluded.expression,
                time_column = excluded.time_column,
                dimensions = excluded.dimensions,
                filters = excluded.filters,
                synonyms = excluded.synonyms,
                notes = excluded.notes,
                sql = excluded.sql,
                sql_variants = excluded.sql_variants,
                validation = excluded.validation,
                source = excluded.source,
                updated_at = excluded.updated_at""",
            [
                id, name, display_name, category, description, type, unit, grain,
                table_name, tables, expression, time_column, dimensions, filters,
                synonyms, notes, sql,
                json_dumps(sql_variants), json_dumps(validation), source,
                now, now,
            ],
        )
        return self.get(id)

    def get(self, metric_id: str) -> Optional[Dict[str, Any]]:
        result = self.conn.execute(
            "SELECT * FROM metric_definitions WHERE id = ?", [metric_id]
        ).fetchone()
        return self._row_to_dict(result)

    def list(self, category: Optional[str] = None) -> List[Dict[str, Any]]:
        if category is not None:
            rows = self.conn.execute(
                "SELECT * FROM metric_definitions WHERE category = ? ORDER BY name",
                [category],
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM metric_definitions ORDER BY name"
            ).fetchall()
        return self._rows_to_dicts(rows)

    def update(self, metric_id: str, **kwargs) -> Optional[Dict[str, Any]]:
        # Check existence first
        existing = self.get(metric_id)
        if existing is None:
            return None

        allowed = {
            "name", "display_name", "category", "description", "type", "unit",
            "grain", "table_name", "tables", "expression", "time_column",
            "dimensions", "filters", "synonyms", "notes", "sql",
            "sql_variants", "validation", "source",
        }
        # JSON fields that need serialization
        json_fields = {"sql_variants", "validation"}

        updates = {}
        for k, v in kwargs.items():
            if k in allowed:
                if k in json_fields:
                    updates[k] = json_dumps(v)
                else:
                    updates[k] = v

        if not updates:
            return existing

        updates["updated_at"] = datetime.now(timezone.utc)
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [metric_id]
        self.conn.execute(
            f"UPDATE metric_definitions SET {set_clause} WHERE id = ?", values
        )
        return self.get(metric_id)

    def delete(self, metric_id: str) -> bool:
        existing = self.get(metric_id)
        if existing is None:
            return False
        self.conn.execute("DELETE FROM metric_definitions WHERE id = ?", [metric_id])
        return True

    def find_by_table(self, table_name: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM metric_definitions WHERE table_name = ? ORDER BY name",
            [table_name],
        ).fetchall()
        return self._rows_to_dicts(rows)

    def find_by_synonym(self, term: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM metric_definitions WHERE list_contains(synonyms, ?) ORDER BY name",
            [term],
        ).fetchall()
        return self._rows_to_dicts(rows)

    def get_table_map(self) -> Dict[str, List[str]]:
        """Return {table_name: [metric_name, ...]} for profiler use."""
        rows = self.conn.execute(
            "SELECT table_name, name FROM metric_definitions WHERE table_name IS NOT NULL ORDER BY table_name, name"
        ).fetchall()
        result: Dict[str, List[str]] = {}
        for table_name, metric_name in rows:
            result.setdefault(table_name, []).append(metric_name)
        return result
