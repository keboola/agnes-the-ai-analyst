"""Repository for metric definitions."""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, List, Dict, Union

import duckdb
import yaml


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
            "SELECT * FROM metric_definitions WHERE table_name = ? OR list_contains(tables, ?) ORDER BY name",
            [table_name, table_name],
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
        # Also include metrics that reference tables via the 'tables' array
        results2 = self.conn.execute(
            "SELECT unnest(tables) AS tbl, name FROM metric_definitions WHERE tables IS NOT NULL"
        ).fetchall()
        for tbl, metric_name in results2:
            result.setdefault(tbl, []).append(metric_name)
        return result

    def import_from_yaml(self, path: Union[str, Path]) -> int:
        """Import metrics from a YAML file or directory of YAML files.

        Args:
            path: Path to a single .yml file or a directory containing */*.yml files.

        Returns:
            Number of metrics imported.
        """
        path = Path(path)
        files: List[Path] = []

        if path.is_file():
            files = [path]
        elif path.is_dir():
            files = sorted(path.glob("*/*.yml"))

        count = 0
        for file_path in files:
            # Infer category from parent directory name
            category_from_dir = file_path.parent.name

            with open(file_path, "r") as f:
                raw = yaml.safe_load(f)

            # Support both list-wrapped [{...}] and plain {...} formats
            if isinstance(raw, list):
                metrics_data = raw
            elif isinstance(raw, dict):
                metrics_data = [raw]
            else:
                continue

            for data in metrics_data:
                if not isinstance(data, dict):
                    continue

                name = data.get("name")
                if not name:
                    continue

                category = data.get("category") or category_from_dir

                # Build id as "category/name"
                metric_id = f"{category}/{name}"

                # Map YAML 'table' -> DB 'table_name'
                table_name = data.get("table") or data.get("table_name")

                # Collect sql_by_* keys into sql_variants dict
                # e.g. sql_by_channel → {"by_channel": "..."}
                sql_variants: Dict[str, str] = {}
                for key, value in data.items():
                    if key.startswith("sql_by_"):
                        variant_key = key[len("sql_"):]  # strip 'sql_' prefix → 'by_channel'
                        sql_variants[variant_key] = value

                self.create(
                    id=metric_id,
                    name=name,
                    display_name=data.get("display_name", name),
                    category=category,
                    sql=data.get("sql", ""),
                    description=data.get("description"),
                    type=data.get("type", "sum"),
                    unit=data.get("unit"),
                    grain=data.get("grain", "monthly"),
                    table_name=table_name,
                    tables=data.get("tables"),
                    expression=data.get("expression"),
                    time_column=data.get("time_column"),
                    dimensions=data.get("dimensions"),
                    filters=data.get("filters"),
                    synonyms=data.get("synonyms"),
                    notes=data.get("notes"),
                    sql_variants=sql_variants if sql_variants else None,
                    validation=data.get("validation"),
                    source="yaml_import",
                )
                count += 1

        return count

    def export_to_yaml(self, output_dir: Union[str, Path]) -> int:
        """Export all metrics to YAML files under output_dir/{category}/{name}.yml.

        Args:
            output_dir: Root directory for the exported YAML files.

        Returns:
            Number of metrics exported.
        """
        output_dir = Path(output_dir)
        metrics = self.list()
        count = 0

        for metric in metrics:
            category = metric.get("category") or "uncategorized"
            name = metric.get("name") or metric["id"].split("/")[-1]

            category_dir = output_dir / category
            category_dir.mkdir(parents=True, exist_ok=True)

            # Build the YAML dict — map table_name back to table
            data: Dict[str, Any] = {"name": name}
            if metric.get("display_name"):
                data["display_name"] = metric["display_name"]
            data["category"] = category
            if metric.get("type"):
                data["type"] = metric["type"]
            if metric.get("unit"):
                data["unit"] = metric["unit"]
            if metric.get("grain"):
                data["grain"] = metric["grain"]
            if metric.get("time_column"):
                data["time_column"] = metric["time_column"]
            # Use 'table' (not 'table_name') in YAML output
            if metric.get("table_name"):
                data["table"] = metric["table_name"]
            if metric.get("expression"):
                data["expression"] = metric["expression"]
            if metric.get("description"):
                data["description"] = metric["description"]
            if metric.get("dimensions"):
                data["dimensions"] = metric["dimensions"]
            if metric.get("filters"):
                data["filters"] = metric["filters"]
            if metric.get("synonyms"):
                data["synonyms"] = metric["synonyms"]
            if metric.get("notes"):
                data["notes"] = metric["notes"]
            if metric.get("sql"):
                data["sql"] = metric["sql"]

            # Expand sql_variants back to sql_by_* keys
            sql_variants = metric.get("sql_variants")
            if sql_variants:
                if isinstance(sql_variants, str):
                    try:
                        sql_variants = json.loads(sql_variants)
                    except (json.JSONDecodeError, ValueError):
                        sql_variants = {}
                if isinstance(sql_variants, dict):
                    for variant_key, variant_sql in sql_variants.items():
                        # variant_key is e.g. 'by_channel' → YAML key 'sql_by_channel'
                        data[f"sql_{variant_key}"] = variant_sql

            # Handle validation JSON
            validation = metric.get("validation")
            if validation:
                if isinstance(validation, str):
                    try:
                        validation = json.loads(validation)
                    except (json.JSONDecodeError, ValueError):
                        validation = None
                if validation:
                    data["validation"] = validation

            out_file = category_dir / f"{name}.yml"
            with open(out_file, "w") as f:
                yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

            count += 1

        return count
