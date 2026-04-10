# Business Metrics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add DuckDB-backed business metrics framework with YAML import/export, CLI, API, profiler integration, and a 10-metric starter pack.

**Architecture:** New `metric_definitions` table in system.duckdb (schema v4). Repository pattern matching `table_registry.py`. CLI commands via Typer, API via FastAPI router. Profiler refactored to read metrics from DuckDB instead of YAML.

**Tech Stack:** DuckDB, FastAPI, Typer, PyYAML, pytest

**Spec:** `docs/superpowers/specs/2026-04-10-porting-internal-features-design.md` — Section 1

---

### Task 1: Schema Migration v3→v4

**Files:**
- Modify: `src/db.py:19` (SCHEMA_VERSION), `src/db.py:258-302` (migrations + _ensure_schema)
- Test: `tests/test_db.py`

- [ ] **Step 1: Write failing test for v4 schema**

In `tests/test_db.py`, add:

```python
class TestSchemaV4:
    def test_metric_definitions_table_exists(self, tmp_path, monkeypatch):
        _setup_data_dir(tmp_path, monkeypatch)
        from src.db import get_system_db

        conn = get_system_db()
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
                ).fetchall()
            }
            assert "metric_definitions" in tables
            assert "column_metadata" in tables
        finally:
            conn.close()

    def test_metric_definitions_columns(self, tmp_path, monkeypatch):
        _setup_data_dir(tmp_path, monkeypatch)
        from src.db import get_system_db

        conn = get_system_db()
        try:
            cols = {
                row[0]
                for row in conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'metric_definitions'"
                ).fetchall()
            }
            expected = {
                "id", "name", "display_name", "category", "description",
                "type", "unit", "grain", "table_name", "tables", "expression",
                "time_column", "dimensions", "filters", "synonyms", "notes",
                "sql", "sql_variants", "validation", "source",
                "created_at", "updated_at",
            }
            assert expected.issubset(cols), f"Missing: {expected - cols}"
        finally:
            conn.close()

    def test_column_metadata_table_exists(self, tmp_path, monkeypatch):
        _setup_data_dir(tmp_path, monkeypatch)
        from src.db import get_system_db

        conn = get_system_db()
        try:
            cols = {
                row[0]
                for row in conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'column_metadata'"
                ).fetchall()
            }
            expected = {"table_id", "column_name", "basetype", "description", "confidence", "source", "updated_at"}
            assert expected.issubset(cols), f"Missing: {expected - cols}"
        finally:
            conn.close()

    def test_v3_to_v4_migration(self, tmp_path, monkeypatch):
        """Simulate a v3 database and verify migration to v4."""
        _setup_data_dir(tmp_path, monkeypatch)
        import duckdb
        from src.db import _SYSTEM_SCHEMA

        db_path = tmp_path / "state" / "system.duckdb"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = duckdb.connect(str(db_path))
        conn.execute(_SYSTEM_SCHEMA)
        conn.execute("INSERT INTO schema_version (version) VALUES (3)")
        conn.close()

        from src.db import get_system_db
        conn = get_system_db()
        try:
            version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
            assert version == 4
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
                ).fetchall()
            }
            assert "metric_definitions" in tables
            assert "column_metadata" in tables
        finally:
            conn.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_db.py::TestSchemaV4 -v`
Expected: FAIL — `metric_definitions` table does not exist

- [ ] **Step 3: Implement schema v4 migration**

In `src/db.py`, change line 19:

```python
SCHEMA_VERSION = 4
```

After `_V2_TO_V3_MIGRATIONS` (line ~260), add:

```python
_V3_TO_V4_MIGRATIONS = [
    """CREATE TABLE IF NOT EXISTS metric_definitions (
        id              VARCHAR PRIMARY KEY,
        name            VARCHAR NOT NULL,
        display_name    VARCHAR NOT NULL,
        category        VARCHAR NOT NULL,
        description     TEXT,
        type            VARCHAR DEFAULT 'sum',
        unit            VARCHAR,
        grain           VARCHAR DEFAULT 'monthly',
        table_name      VARCHAR,
        tables          VARCHAR[],
        expression      VARCHAR,
        time_column     VARCHAR,
        dimensions      VARCHAR[],
        filters         VARCHAR[],
        synonyms        VARCHAR[],
        notes           VARCHAR[],
        sql             TEXT NOT NULL,
        sql_variants    JSON,
        validation      JSON,
        source          VARCHAR DEFAULT 'manual',
        created_at      TIMESTAMP DEFAULT current_timestamp,
        updated_at      TIMESTAMP DEFAULT current_timestamp
    )""",
    """CREATE TABLE IF NOT EXISTS column_metadata (
        table_id        VARCHAR NOT NULL,
        column_name     VARCHAR NOT NULL,
        basetype        VARCHAR,
        description     VARCHAR,
        confidence      VARCHAR DEFAULT 'manual',
        source          VARCHAR DEFAULT 'manual',
        updated_at      TIMESTAMP DEFAULT current_timestamp,
        PRIMARY KEY (table_id, column_name)
    )""",
]
```

In `_ensure_schema()`, after the `if current < 3:` block (line ~298), add:

```python
            if current < 4:
                for sql in _V3_TO_V4_MIGRATIONS:
                    conn.execute(sql)
```

Also update the `test_creates_all_tables` test's `expected` set to include `"metric_definitions"` and `"column_metadata"`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_db.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add src/db.py tests/test_db.py
git commit -m "feat: add schema v4 with metric_definitions and column_metadata tables"
```

---

### Task 2: MetricRepository — CRUD

**Files:**
- Create: `src/repositories/metrics.py`
- Test: `tests/test_metrics.py`

- [ ] **Step 1: Write failing tests for MetricRepository CRUD**

Create `tests/test_metrics.py`:

```python
"""Tests for MetricRepository."""

import os
import pytest
import duckdb


@pytest.fixture
def db_conn(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.db import get_system_db
    conn = get_system_db()
    yield conn
    conn.close()


SAMPLE_METRIC = {
    "id": "revenue/mrr",
    "name": "mrr",
    "display_name": "Monthly Recurring Revenue",
    "category": "revenue",
    "description": "Total MRR from all subscriptions",
    "type": "sum",
    "unit": "USD",
    "grain": "monthly",
    "table_name": "subscriptions",
    "expression": "SUM(mrr_amount)",
    "time_column": "billing_date",
    "dimensions": ["plan_type", "region"],
    "synonyms": ["monthly_revenue", "recurring_revenue"],
    "notes": ["Excludes one-time fees"],
    "sql": "SELECT DATE_TRUNC('month', billing_date) AS month, SUM(mrr_amount) AS mrr FROM subscriptions GROUP BY 1",
}


class TestMetricRepositoryCreate:
    def test_create_metric(self, db_conn):
        from src.repositories.metrics import MetricRepository
        repo = MetricRepository(db_conn)
        result = repo.create(**SAMPLE_METRIC)
        assert result["id"] == "revenue/mrr"
        assert result["name"] == "mrr"

    def test_create_duplicate_upserts(self, db_conn):
        from src.repositories.metrics import MetricRepository
        repo = MetricRepository(db_conn)
        repo.create(**SAMPLE_METRIC)
        updated = {**SAMPLE_METRIC, "display_name": "Updated MRR"}
        result = repo.create(**updated)
        assert result["display_name"] == "Updated MRR"
        assert len(repo.list()) == 1


class TestMetricRepositoryRead:
    def test_get_existing(self, db_conn):
        from src.repositories.metrics import MetricRepository
        repo = MetricRepository(db_conn)
        repo.create(**SAMPLE_METRIC)
        result = repo.get("revenue/mrr")
        assert result is not None
        assert result["name"] == "mrr"
        assert result["dimensions"] == ["plan_type", "region"]

    def test_get_missing(self, db_conn):
        from src.repositories.metrics import MetricRepository
        repo = MetricRepository(db_conn)
        assert repo.get("nonexistent/metric") is None

    def test_list_all(self, db_conn):
        from src.repositories.metrics import MetricRepository
        repo = MetricRepository(db_conn)
        repo.create(**SAMPLE_METRIC)
        repo.create(
            id="revenue/arr", name="arr", display_name="ARR",
            category="revenue", sql="SELECT 1",
        )
        results = repo.list()
        assert len(results) == 2

    def test_list_by_category(self, db_conn):
        from src.repositories.metrics import MetricRepository
        repo = MetricRepository(db_conn)
        repo.create(**SAMPLE_METRIC)
        repo.create(
            id="ops/resolution_time", name="resolution_time",
            display_name="Resolution Time", category="ops",
            sql="SELECT 1",
        )
        results = repo.list(category="revenue")
        assert len(results) == 1
        assert results[0]["name"] == "mrr"


class TestMetricRepositoryUpdate:
    def test_update_fields(self, db_conn):
        from src.repositories.metrics import MetricRepository
        repo = MetricRepository(db_conn)
        repo.create(**SAMPLE_METRIC)
        result = repo.update("revenue/mrr", display_name="Gross MRR", unit="EUR")
        assert result["display_name"] == "Gross MRR"
        assert result["unit"] == "EUR"
        assert result["name"] == "mrr"  # unchanged

    def test_update_missing_returns_none(self, db_conn):
        from src.repositories.metrics import MetricRepository
        repo = MetricRepository(db_conn)
        assert repo.update("nonexistent/x", name="y") is None


class TestMetricRepositoryDelete:
    def test_delete_existing(self, db_conn):
        from src.repositories.metrics import MetricRepository
        repo = MetricRepository(db_conn)
        repo.create(**SAMPLE_METRIC)
        assert repo.delete("revenue/mrr") is True
        assert repo.get("revenue/mrr") is None

    def test_delete_missing(self, db_conn):
        from src.repositories.metrics import MetricRepository
        repo = MetricRepository(db_conn)
        assert repo.delete("nonexistent/x") is False


class TestMetricRepositorySearch:
    def test_find_by_table(self, db_conn):
        from src.repositories.metrics import MetricRepository
        repo = MetricRepository(db_conn)
        repo.create(**SAMPLE_METRIC)
        repo.create(
            id="revenue/arr", name="arr", display_name="ARR",
            category="revenue", table_name="subscriptions",
            sql="SELECT 1",
        )
        repo.create(
            id="ops/tickets", name="tickets", display_name="Tickets",
            category="ops", table_name="tickets",
            sql="SELECT 1",
        )
        results = repo.find_by_table("subscriptions")
        assert len(results) == 2
        names = {r["name"] for r in results}
        assert names == {"mrr", "arr"}

    def test_find_by_synonym(self, db_conn):
        from src.repositories.metrics import MetricRepository
        repo = MetricRepository(db_conn)
        repo.create(**SAMPLE_METRIC)
        results = repo.find_by_synonym("recurring_revenue")
        assert len(results) == 1
        assert results[0]["name"] == "mrr"

    def test_get_table_map(self, db_conn):
        from src.repositories.metrics import MetricRepository
        repo = MetricRepository(db_conn)
        repo.create(**SAMPLE_METRIC)
        repo.create(
            id="ops/tickets", name="tickets", display_name="Tickets",
            category="ops", table_name="tickets",
            sql="SELECT 1",
        )
        table_map = repo.get_table_map()
        assert table_map == {"subscriptions": ["mrr"], "tickets": ["tickets"]}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_metrics.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.repositories.metrics'`

- [ ] **Step 3: Implement MetricRepository**

Create `src/repositories/metrics.py`:

```python
"""Repository for metric definitions."""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import duckdb


class MetricRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def create(self, id: str, name: str, display_name: str, category: str,
               sql: str, description: Optional[str] = None,
               type: str = "sum", unit: Optional[str] = None,
               grain: str = "monthly", table_name: Optional[str] = None,
               tables: Optional[List[str]] = None, expression: Optional[str] = None,
               time_column: Optional[str] = None, dimensions: Optional[List[str]] = None,
               filters: Optional[List[str]] = None, synonyms: Optional[List[str]] = None,
               notes: Optional[List[str]] = None, sql_variants: Optional[dict] = None,
               validation: Optional[dict] = None, source: str = "manual",
               **kwargs) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """INSERT INTO metric_definitions (
                id, name, display_name, category, description, type, unit, grain,
                table_name, tables, expression, time_column, dimensions, filters,
                synonyms, notes, sql, sql_variants, validation, source,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET
                name = excluded.name, display_name = excluded.display_name,
                category = excluded.category, description = excluded.description,
                type = excluded.type, unit = excluded.unit, grain = excluded.grain,
                table_name = excluded.table_name, tables = excluded.tables,
                expression = excluded.expression, time_column = excluded.time_column,
                dimensions = excluded.dimensions, filters = excluded.filters,
                synonyms = excluded.synonyms, notes = excluded.notes,
                sql = excluded.sql, sql_variants = excluded.sql_variants,
                validation = excluded.validation, source = excluded.source,
                updated_at = excluded.updated_at""",
            [id, name, display_name, category, description, type, unit, grain,
             table_name, tables, expression, time_column, dimensions, filters,
             synonyms, notes, sql,
             json_dumps(sql_variants), json_dumps(validation),
             source, now, now],
        )
        return self.get(id)

    def get(self, metric_id: str) -> Optional[Dict[str, Any]]:
        result = self.conn.execute(
            "SELECT * FROM metric_definitions WHERE id = ?", [metric_id]
        ).fetchone()
        if not result:
            return None
        columns = [desc[0] for desc in self.conn.description]
        return dict(zip(columns, result))

    def list(self, category: Optional[str] = None) -> List[Dict[str, Any]]:
        if category:
            results = self.conn.execute(
                "SELECT * FROM metric_definitions WHERE category = ? ORDER BY name",
                [category],
            ).fetchall()
        else:
            results = self.conn.execute(
                "SELECT * FROM metric_definitions ORDER BY category, name"
            ).fetchall()
        if not results:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, row)) for row in results]

    def update(self, metric_id: str, **kwargs) -> Optional[Dict[str, Any]]:
        existing = self.get(metric_id)
        if not existing:
            return None
        kwargs["updated_at"] = datetime.now(timezone.utc)
        # Convert JSON fields
        for json_field in ("sql_variants", "validation"):
            if json_field in kwargs and kwargs[json_field] is not None:
                kwargs[json_field] = json_dumps(kwargs[json_field])
        set_clauses = ", ".join(f"{k} = ?" for k in kwargs)
        values = list(kwargs.values()) + [metric_id]
        self.conn.execute(
            f"UPDATE metric_definitions SET {set_clauses} WHERE id = ?",
            values,
        )
        return self.get(metric_id)

    def delete(self, metric_id: str) -> bool:
        existing = self.get(metric_id)
        if not existing:
            return False
        self.conn.execute("DELETE FROM metric_definitions WHERE id = ?", [metric_id])
        return True

    def find_by_table(self, table_name: str) -> List[Dict[str, Any]]:
        results = self.conn.execute(
            "SELECT * FROM metric_definitions WHERE table_name = ? ORDER BY name",
            [table_name],
        ).fetchall()
        if not results:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, row)) for row in results]

    def find_by_synonym(self, term: str) -> List[Dict[str, Any]]:
        results = self.conn.execute(
            "SELECT * FROM metric_definitions WHERE list_contains(synonyms, ?) ORDER BY name",
            [term],
        ).fetchall()
        if not results:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [dict(zip(columns, row)) for row in results]

    def get_table_map(self) -> Dict[str, List[str]]:
        """Return {table_name: [metric_name, ...]} for profiler integration."""
        results = self.conn.execute(
            "SELECT table_name, name FROM metric_definitions WHERE table_name IS NOT NULL ORDER BY table_name"
        ).fetchall()
        table_map: Dict[str, List[str]] = {}
        for table_name, metric_name in results:
            table_map.setdefault(table_name, []).append(metric_name)
        return table_map


def json_dumps(obj) -> Optional[str]:
    """Serialize to JSON string for DuckDB JSON columns, or None."""
    if obj is None:
        return None
    import json
    return json.dumps(obj)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_metrics.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add src/repositories/metrics.py tests/test_metrics.py
git commit -m "feat: add MetricRepository with CRUD, search, and table map"
```

---

### Task 3: YAML Import/Export

**Files:**
- Modify: `src/repositories/metrics.py` (add import_from_yaml, export_to_yaml)
- Test: `tests/test_metrics.py` (add import/export tests)

- [ ] **Step 1: Write failing tests for YAML import/export**

Add to `tests/test_metrics.py`:

```python
import yaml
from pathlib import Path


@pytest.fixture
def metrics_dir(tmp_path):
    """Create a sample metrics directory with YAML files."""
    revenue_dir = tmp_path / "metrics" / "revenue"
    revenue_dir.mkdir(parents=True)
    ops_dir = tmp_path / "metrics" / "operations"
    ops_dir.mkdir(parents=True)

    # total_revenue.yml — uses 'table' key (YAML format)
    (revenue_dir / "total_revenue.yml").write_text(yaml.dump({
        "name": "total_revenue",
        "display_name": "Total Revenue",
        "category": "revenue",
        "type": "sum",
        "unit": "USD",
        "grain": "monthly",
        "table": "orders",
        "expression": "SUM(total_amount)",
        "time_column": "order_date",
        "dimensions": ["channel", "region"],
        "synonyms": ["gross_revenue"],
        "sql": "SELECT SUM(total_amount) FROM orders",
        "sql_by_channel": "SELECT channel, SUM(total_amount) FROM orders GROUP BY 1",
    }))

    # resolution_time.yml
    (ops_dir / "resolution_time.yml").write_text(yaml.dump({
        "name": "resolution_time",
        "display_name": "Support Resolution Time",
        "category": "operations",
        "type": "avg",
        "unit": "hours",
        "table": "tickets",
        "sql": "SELECT AVG(resolution_hours) FROM tickets",
    }))

    return tmp_path / "metrics"


class TestMetricRepositoryImport:
    def test_import_from_directory(self, db_conn, metrics_dir):
        from src.repositories.metrics import MetricRepository
        repo = MetricRepository(db_conn)
        count = repo.import_from_yaml(metrics_dir)
        assert count == 2
        assert repo.get("revenue/total_revenue") is not None
        assert repo.get("operations/resolution_time") is not None

    def test_import_maps_table_to_table_name(self, db_conn, metrics_dir):
        from src.repositories.metrics import MetricRepository
        repo = MetricRepository(db_conn)
        repo.import_from_yaml(metrics_dir)
        metric = repo.get("revenue/total_revenue")
        assert metric["table_name"] == "orders"

    def test_import_collects_sql_variants(self, db_conn, metrics_dir):
        from src.repositories.metrics import MetricRepository
        repo = MetricRepository(db_conn)
        repo.import_from_yaml(metrics_dir)
        metric = repo.get("revenue/total_revenue")
        variants = metric["sql_variants"]
        # DuckDB returns JSON as string or dict depending on version
        if isinstance(variants, str):
            import json
            variants = json.loads(variants)
        assert "by_channel" in variants

    def test_import_single_file(self, db_conn, metrics_dir):
        from src.repositories.metrics import MetricRepository
        repo = MetricRepository(db_conn)
        count = repo.import_from_yaml(metrics_dir / "revenue" / "total_revenue.yml")
        assert count == 1

    def test_import_idempotent(self, db_conn, metrics_dir):
        from src.repositories.metrics import MetricRepository
        repo = MetricRepository(db_conn)
        repo.import_from_yaml(metrics_dir)
        repo.import_from_yaml(metrics_dir)
        assert len(repo.list()) == 2


class TestMetricRepositoryExport:
    def test_export_to_yaml(self, db_conn, metrics_dir, tmp_path):
        from src.repositories.metrics import MetricRepository
        repo = MetricRepository(db_conn)
        repo.import_from_yaml(metrics_dir)

        export_dir = tmp_path / "export"
        count = repo.export_to_yaml(export_dir)
        assert count == 2

        # Check directory structure
        assert (export_dir / "revenue" / "total_revenue.yml").exists()
        assert (export_dir / "operations" / "resolution_time.yml").exists()

        # Check content
        data = yaml.safe_load((export_dir / "revenue" / "total_revenue.yml").read_text())
        assert data["name"] == "total_revenue"
        assert data["table"] == "orders"  # exported as 'table', not 'table_name'
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_metrics.py::TestMetricRepositoryImport -v`
Expected: FAIL — `AttributeError: 'MetricRepository' object has no attribute 'import_from_yaml'`

- [ ] **Step 3: Implement import_from_yaml and export_to_yaml**

Add to `MetricRepository` class in `src/repositories/metrics.py`:

```python
    def import_from_yaml(self, path) -> int:
        """Import metrics from YAML file(s). Returns count imported.

        Args:
            path: Path to a single YAML file or directory containing category/metric.yml files.
        """
        from pathlib import Path
        import yaml

        path = Path(path)
        count = 0

        if path.is_file():
            yml_files = [path]
        elif path.is_dir():
            yml_files = sorted(path.glob("*/*.yml"))
        else:
            return 0

        for yml_file in yml_files:
            try:
                with open(yml_file) as f:
                    data = yaml.safe_load(f)
            except (yaml.YAMLError, OSError):
                continue

            if not data:
                continue

            # Handle list-wrapped metrics (internal repo format)
            metric_list = data if isinstance(data, list) else [data]
            for metric in metric_list:
                if not isinstance(metric, dict) or "name" not in metric:
                    continue

                # Infer category from directory name
                category = metric.get("category", yml_file.parent.name)
                name = metric["name"]
                metric_id = f"{category}/{name}"

                # Map YAML 'table' → DuckDB 'table_name'
                table_name = metric.pop("table", None)

                # Collect sql_by_* variants
                sql_variants = {}
                keys_to_remove = []
                for key in list(metric.keys()):
                    if key.startswith("sql_by_"):
                        variant_name = key[4:]  # "sql_by_channel" → "by_channel"
                        sql_variants[variant_name] = metric[key]
                        keys_to_remove.append(key)
                for key in keys_to_remove:
                    del metric[key]

                self.create(
                    id=metric_id,
                    name=name,
                    display_name=metric.get("display_name", name),
                    category=category,
                    description=metric.get("description"),
                    type=metric.get("type", "sum"),
                    unit=metric.get("unit"),
                    grain=metric.get("grain", "monthly"),
                    table_name=table_name or metric.get("table_name"),
                    tables=metric.get("tables"),
                    expression=metric.get("expression"),
                    time_column=metric.get("time_column"),
                    dimensions=metric.get("dimensions"),
                    filters=metric.get("filters"),
                    synonyms=metric.get("synonyms"),
                    notes=metric.get("notes"),
                    sql=metric.get("sql", ""),
                    sql_variants=sql_variants if sql_variants else None,
                    validation=metric.get("validation"),
                    source="yaml_import",
                )
                count += 1

        return count

    def export_to_yaml(self, output_dir) -> int:
        """Export all metrics to YAML files. Returns count exported."""
        from pathlib import Path
        import yaml
        import json

        output_dir = Path(output_dir)
        count = 0

        for metric in self.list():
            category = metric["category"]
            name = metric["name"]
            cat_dir = output_dir / category
            cat_dir.mkdir(parents=True, exist_ok=True)

            # Build YAML-compatible dict
            data = {
                "name": name,
                "display_name": metric["display_name"],
                "category": category,
            }

            # Map DuckDB 'table_name' back to YAML 'table'
            if metric.get("table_name"):
                data["table"] = metric["table_name"]

            for field in ("description", "type", "unit", "grain", "tables",
                          "expression", "time_column", "dimensions", "filters",
                          "synonyms", "notes"):
                if metric.get(field) is not None:
                    data[field] = metric[field]

            if metric.get("sql"):
                data["sql"] = metric["sql"]

            # Expand sql_variants back to sql_by_* keys
            variants = metric.get("sql_variants")
            if variants:
                if isinstance(variants, str):
                    variants = json.loads(variants)
                for key, val in variants.items():
                    data[f"sql_{key}"] = val

            if metric.get("validation"):
                val = metric["validation"]
                if isinstance(val, str):
                    val = json.loads(val)
                data["validation"] = val

            yml_path = cat_dir / f"{name}.yml"
            yml_path.write_text(yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False))
            count += 1

        return count
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_metrics.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add src/repositories/metrics.py tests/test_metrics.py
git commit -m "feat: add YAML import/export to MetricRepository"
```

---

### Task 4: CLI `da metrics`

**Files:**
- Create: `cli/commands/metrics.py`
- Modify: `cli/main.py` (register metrics_app)
- Test: `tests/test_cli.py` (add metrics help test)

- [ ] **Step 1: Write failing test for CLI registration**

Add to `tests/test_cli.py` in `TestCLIHelp`:

```python
    def test_metrics_help(self):
        result = runner.invoke(app, ["metrics", "--help"])
        assert result.exit_code == 0
        assert "list" in result.output
        assert "show" in result.output
        assert "import" in result.output
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cli.py::TestCLIHelp::test_metrics_help -v`
Expected: FAIL — `No such command 'metrics'`

- [ ] **Step 3: Implement CLI commands**

Create `cli/commands/metrics.py`:

```python
"""Metrics commands — da metrics."""

import json
from pathlib import Path

import typer

from cli.client import api_get

metrics_app = typer.Typer(help="Business metrics — list, show, import, export, validate")


@metrics_app.command("list")
def list_metrics(
    category: str = typer.Option(None, "--category", "-c", help="Filter by category"),
    as_json: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """List all metrics."""
    params = {}
    if category:
        params["category"] = category
    resp = api_get("/api/metrics", params=params)
    if resp.status_code != 200:
        typer.echo(f"Failed: {resp.json().get('detail', resp.text)}", err=True)
        raise typer.Exit(1)

    data = resp.json()
    metrics = data.get("metrics", [])

    if as_json:
        typer.echo(json.dumps(metrics, indent=2))
    else:
        if not metrics:
            typer.echo("No metrics found. Import with: da metrics import docs/metrics/")
            return
        current_cat = None
        for m in metrics:
            if m["category"] != current_cat:
                current_cat = m["category"]
                typer.echo(f"\n  {current_cat.upper()}")
            typer.echo(f"    {m['id']:40s} {m.get('display_name', m['name'])}")
        typer.echo(f"\nTotal: {len(metrics)} metrics")


@metrics_app.command("show")
def show_metric(
    metric_id: str = typer.Argument(..., help="Metric ID (e.g., revenue/mrr)"),
    as_json: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Show detail for a metric."""
    resp = api_get(f"/api/metrics/{metric_id}")
    if resp.status_code == 404:
        typer.echo(f"Metric not found: {metric_id}", err=True)
        raise typer.Exit(1)
    if resp.status_code != 200:
        typer.echo(f"Failed: {resp.json().get('detail', resp.text)}", err=True)
        raise typer.Exit(1)

    m = resp.json()
    if as_json:
        typer.echo(json.dumps(m, indent=2))
    else:
        typer.echo(f"  {m['display_name']}  ({m['id']})")
        typer.echo(f"  Type: {m.get('type', 'sum')}  |  Unit: {m.get('unit', '-')}  |  Grain: {m.get('grain', '-')}")
        if m.get("description"):
            typer.echo(f"\n  {m['description']}")
        if m.get("table_name"):
            typer.echo(f"\n  Table: {m['table_name']}")
        if m.get("dimensions"):
            typer.echo(f"  Dimensions: {', '.join(m['dimensions'])}")
        if m.get("notes"):
            typer.echo("\n  Notes:")
            for note in m["notes"]:
                typer.echo(f"    - {note}")
        if m.get("sql"):
            typer.echo(f"\n  SQL:\n    {m['sql']}")


@metrics_app.command("import")
def import_metrics(
    path: str = typer.Argument(..., help="Path to YAML file or directory"),
):
    """Import metrics from YAML into DuckDB."""
    from src.db import get_system_db
    from src.repositories.metrics import MetricRepository

    source = Path(path)
    if not source.exists():
        typer.echo(f"Path not found: {path}", err=True)
        raise typer.Exit(1)

    conn = get_system_db()
    try:
        repo = MetricRepository(conn)
        count = repo.import_from_yaml(source)
        typer.echo(f"Imported {count} metrics into DuckDB")
    finally:
        conn.close()


@metrics_app.command("export")
def export_metrics(
    output_dir: str = typer.Option("./metrics_export", "--dir", "-d", help="Output directory"),
):
    """Export metrics from DuckDB to YAML files."""
    from src.db import get_system_db
    from src.repositories.metrics import MetricRepository

    conn = get_system_db()
    try:
        repo = MetricRepository(conn)
        count = repo.export_to_yaml(output_dir)
        typer.echo(f"Exported {count} metrics to {output_dir}/")
    finally:
        conn.close()


@metrics_app.command("validate")
def validate_metrics():
    """Validate metrics — check that referenced tables exist in analytics DB."""
    from src.db import get_system_db
    from src.repositories.metrics import MetricRepository
    from src.repositories.table_registry import TableRegistryRepository

    conn = get_system_db()
    try:
        metrics_repo = MetricRepository(conn)
        tables_repo = TableRegistryRepository(conn)
        all_tables = {t["id"] for t in tables_repo.list_all()}
        metrics = metrics_repo.list()

        ok = 0
        warnings = 0
        for m in metrics:
            if m.get("table_name") and m["table_name"] not in all_tables:
                typer.echo(f"  WARN  {m['id']}: table '{m['table_name']}' not in registry")
                warnings += 1
            else:
                ok += 1

        typer.echo(f"\nValidated {len(metrics)} metrics: {ok} OK, {warnings} warnings")
    finally:
        conn.close()
```

Register in `cli/main.py` — add import and registration:

```python
from cli.commands.metrics import metrics_app
# ...
app.add_typer(metrics_app, name="metrics")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_cli.py::TestCLIHelp::test_metrics_help -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add cli/commands/metrics.py cli/main.py tests/test_cli.py
git commit -m "feat: add da metrics CLI commands (list, show, import, export, validate)"
```

---

### Task 5: API Endpoints

**Files:**
- Create: `app/api/metrics.py`
- Modify: `app/main.py` (register router)
- Modify: `app/api/catalog.py` (deprecation redirect)
- Test: `tests/test_api.py` (add metrics API tests)

- [ ] **Step 1: Write failing tests for metrics API**

Add to `tests/test_api.py`:

```python
class TestMetricsAPI:
    def test_list_metrics_empty(self, seeded_client):
        client, admin_token, _ = seeded_client
        resp = client.get("/api/metrics", headers={"Authorization": f"Bearer {admin_token}"})
        assert resp.status_code == 200
        assert resp.json()["metrics"] == []

    def test_create_and_list_metric(self, seeded_client):
        client, admin_token, _ = seeded_client
        metric = {
            "id": "revenue/mrr",
            "name": "mrr",
            "display_name": "MRR",
            "category": "revenue",
            "sql": "SELECT SUM(amount) FROM subscriptions",
        }
        resp = client.post(
            "/api/admin/metrics",
            json=metric,
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert resp.status_code == 201

        resp = client.get("/api/metrics", headers={"Authorization": f"Bearer {admin_token}"})
        assert resp.status_code == 200
        assert len(resp.json()["metrics"]) == 1

    def test_get_metric_detail(self, seeded_client):
        client, admin_token, _ = seeded_client
        client.post(
            "/api/admin/metrics",
            json={"id": "revenue/mrr", "name": "mrr", "display_name": "MRR",
                  "category": "revenue", "sql": "SELECT 1"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        resp = client.get("/api/metrics/revenue/mrr", headers={"Authorization": f"Bearer {admin_token}"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "mrr"

    def test_get_metric_not_found(self, seeded_client):
        client, admin_token, _ = seeded_client
        resp = client.get("/api/metrics/nonexistent/x", headers={"Authorization": f"Bearer {admin_token}"})
        assert resp.status_code == 404

    def test_delete_metric(self, seeded_client):
        client, admin_token, _ = seeded_client
        client.post(
            "/api/admin/metrics",
            json={"id": "revenue/mrr", "name": "mrr", "display_name": "MRR",
                  "category": "revenue", "sql": "SELECT 1"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        resp = client.delete("/api/admin/metrics/revenue/mrr",
                             headers={"Authorization": f"Bearer {admin_token}"})
        assert resp.status_code == 200

    def test_analyst_cannot_create_metric(self, seeded_client):
        client, _, analyst_token = seeded_client
        resp = client.post(
            "/api/admin/metrics",
            json={"id": "revenue/mrr", "name": "mrr", "display_name": "MRR",
                  "category": "revenue", "sql": "SELECT 1"},
            headers={"Authorization": f"Bearer {analyst_token}"},
        )
        assert resp.status_code == 403

    def test_list_metrics_filter_by_category(self, seeded_client):
        client, admin_token, _ = seeded_client
        for m in [
            {"id": "revenue/mrr", "name": "mrr", "display_name": "MRR", "category": "revenue", "sql": "SELECT 1"},
            {"id": "ops/tickets", "name": "tickets", "display_name": "Tickets", "category": "ops", "sql": "SELECT 1"},
        ]:
            client.post("/api/admin/metrics", json=m, headers={"Authorization": f"Bearer {admin_token}"})
        resp = client.get("/api/metrics?category=revenue", headers={"Authorization": f"Bearer {admin_token}"})
        assert len(resp.json()["metrics"]) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_api.py::TestMetricsAPI -v`
Expected: FAIL — 404 on `/api/metrics`

- [ ] **Step 3: Implement API router**

Create `app/api/metrics.py`:

```python
"""Metrics API endpoints."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
import duckdb

from app.auth.dependencies import get_current_user, require_admin, _get_db
from src.repositories.metrics import MetricRepository

router = APIRouter(tags=["metrics"])


class MetricCreate(BaseModel):
    id: str
    name: str
    display_name: str
    category: str
    sql: str
    description: Optional[str] = None
    type: str = "sum"
    unit: Optional[str] = None
    grain: str = "monthly"
    table_name: Optional[str] = None
    tables: Optional[list] = None
    expression: Optional[str] = None
    time_column: Optional[str] = None
    dimensions: Optional[list] = None
    filters: Optional[list] = None
    synonyms: Optional[list] = None
    notes: Optional[list] = None
    sql_variants: Optional[dict] = None
    validation: Optional[dict] = None


@router.get("/api/metrics")
async def list_metrics(
    category: Optional[str] = Query(None),
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = MetricRepository(conn)
    metrics = repo.list(category=category)
    return {"metrics": metrics, "count": len(metrics)}


@router.get("/api/metrics/{metric_id:path}")
async def get_metric(
    metric_id: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = MetricRepository(conn)
    metric = repo.get(metric_id)
    if not metric:
        raise HTTPException(status_code=404, detail=f"Metric not found: {metric_id}")
    return metric


@router.post("/api/admin/metrics", status_code=201)
async def create_metric(
    body: MetricCreate,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = MetricRepository(conn)
    return repo.create(**body.model_dump())


@router.delete("/api/admin/metrics/{metric_id:path}")
async def delete_metric(
    metric_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = MetricRepository(conn)
    if not repo.delete(metric_id):
        raise HTTPException(status_code=404, detail=f"Metric not found: {metric_id}")
    return {"status": "deleted", "id": metric_id}
```

Register in `app/main.py` — add import and include:

```python
from app.api.metrics import router as metrics_router
# ... (add near other router imports at top of file)

# In create_app(), add before web_router:
app.include_router(metrics_router)
```

Deprecate old endpoint in `app/api/catalog.py` — replace the `get_metric` function:

```python
@router.get("/metrics/{metric_path:path}", deprecated=True)
async def get_metric(
    metric_path: str,
    user: dict = Depends(get_current_user),
):
    """Deprecated: Use GET /api/metrics/{metric_id} instead."""
    from fastapi.responses import RedirectResponse
    # Strip .yml extension for the new endpoint
    metric_id = metric_path.replace(".yml", "")
    return RedirectResponse(url=f"/api/metrics/{metric_id}", status_code=301)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_api.py::TestMetricsAPI -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add app/api/metrics.py app/main.py app/api/catalog.py tests/test_api.py
git commit -m "feat: add metrics API endpoints with admin CRUD"
```

---

### Task 6: Starter Pack Metrics

**Files:**
- Create: `docs/metrics/metrics.yml` (index)
- Create: `docs/metrics/revenue/mrr.yml`
- Create: `docs/metrics/revenue/arr.yml`
- Create: `docs/metrics/revenue/churn_rate.yml`
- Create: `docs/metrics/product_usage/active_users.yml`
- Create: `docs/metrics/product_usage/feature_adoption.yml`
- Create: `docs/metrics/sales/new_customers.yml`
- Create: `docs/metrics/sales/upsell_expansion.yml`
- Create: `docs/metrics/sales/pipeline_value.yml`
- Create: `docs/metrics/operations/support_resolution_time.yml`
- Create: `docs/metrics/operations/infrastructure_cost.yml`
- Existing: `docs/metrics/revenue/total_revenue.yml` (already exists, no change)

- [ ] **Step 1: Create metrics index**

Create `docs/metrics/metrics.yml`:

```yaml
version: "2.0"
description: "Business metrics starter pack. Import with: da metrics import docs/metrics/"
categories:
  - name: revenue
    folder: revenue/
    metrics: [total_revenue, mrr, arr, churn_rate]
  - name: product_usage
    folder: product_usage/
    metrics: [active_users, feature_adoption]
  - name: sales
    folder: sales/
    metrics: [new_customers, upsell_expansion, pipeline_value]
  - name: operations
    folder: operations/
    metrics: [support_resolution_time, infrastructure_cost]
```

- [ ] **Step 2: Create revenue metrics**

Create `docs/metrics/revenue/mrr.yml`:

```yaml
- name: mrr
  display_name: Monthly Recurring Revenue
  category: revenue
  type: sum
  unit: USD
  grain: monthly
  table: subscriptions
  expression: "SUM(mrr_amount)"
  time_column: billing_date
  dimensions:
    - plan_type
    - region
  notes:
    - "Aggregated at company level, not per-contract"
    - "Excludes one-time setup fees and overages"
  synonyms:
    - monthly_revenue
    - recurring_revenue
  sql: |
    SELECT
        DATE_TRUNC('month', billing_date) AS month,
        SUM(mrr_amount) AS mrr
    FROM subscriptions
    WHERE status = 'active'
    GROUP BY 1
    ORDER BY 1
  sql_by_plan: |
    SELECT
        DATE_TRUNC('month', billing_date) AS month,
        plan_type,
        SUM(mrr_amount) AS mrr
    FROM subscriptions
    WHERE status = 'active'
    GROUP BY 1, 2
    ORDER BY 1, 3 DESC
```

Create `docs/metrics/revenue/arr.yml`:

```yaml
- name: arr
  display_name: Annual Recurring Revenue
  category: revenue
  type: sum
  unit: USD
  grain: monthly
  table: subscriptions
  expression: "SUM(mrr_amount) * 12"
  time_column: billing_date
  notes:
    - "ARR = MRR × 12 (annualized current MRR)"
    - "Point-in-time snapshot, not forward-looking"
  synonyms:
    - annual_revenue
    - annualized_revenue
  sql: |
    SELECT
        DATE_TRUNC('month', billing_date) AS month,
        SUM(mrr_amount) * 12 AS arr
    FROM subscriptions
    WHERE status = 'active'
    GROUP BY 1
    ORDER BY 1
```

Create `docs/metrics/revenue/churn_rate.yml`:

```yaml
- name: churn_rate
  display_name: Monthly Churn Rate
  category: revenue
  type: ratio
  unit: percentage
  grain: monthly
  table: subscriptions
  expression: "churned_mrr / beginning_mrr * 100"
  time_column: billing_date
  notes:
    - "Gross revenue churn — does not net out expansion"
    - "Beginning MRR is MRR at start of month"
  synonyms:
    - revenue_churn
    - mrr_churn
  sql: |
    WITH monthly AS (
        SELECT
            DATE_TRUNC('month', cancelled_at) AS month,
            SUM(mrr_amount) AS churned_mrr
        FROM subscriptions
        WHERE status = 'cancelled'
        GROUP BY 1
    ),
    beginning AS (
        SELECT
            DATE_TRUNC('month', billing_date) AS month,
            SUM(mrr_amount) AS beginning_mrr
        FROM subscriptions
        WHERE status = 'active'
        GROUP BY 1
    )
    SELECT
        b.month,
        COALESCE(m.churned_mrr, 0) / NULLIF(b.beginning_mrr, 0) * 100 AS churn_rate
    FROM beginning b
    LEFT JOIN monthly m ON b.month = m.month
    ORDER BY 1
```

- [ ] **Step 3: Create product_usage metrics**

Create `docs/metrics/product_usage/active_users.yml`:

```yaml
- name: active_users
  display_name: Monthly Active Users
  category: product_usage
  type: count
  unit: count
  grain: monthly
  table: user_events
  expression: "COUNT(DISTINCT user_id)"
  time_column: event_date
  dimensions:
    - feature
    - plan_type
  synonyms:
    - mau
    - monthly_active
  sql: |
    SELECT
        DATE_TRUNC('month', event_date) AS month,
        COUNT(DISTINCT user_id) AS active_users
    FROM user_events
    GROUP BY 1
    ORDER BY 1
```

Create `docs/metrics/product_usage/feature_adoption.yml`:

```yaml
- name: feature_adoption
  display_name: Feature Adoption Rate
  category: product_usage
  type: ratio
  unit: percentage
  grain: monthly
  table: user_events
  expression: "users_using_feature / total_active_users * 100"
  time_column: event_date
  dimensions:
    - feature
  synonyms:
    - adoption_rate
    - feature_usage
  sql: |
    WITH feature_users AS (
        SELECT
            DATE_TRUNC('month', event_date) AS month,
            feature,
            COUNT(DISTINCT user_id) AS users_using
        FROM user_events
        GROUP BY 1, 2
    ),
    total AS (
        SELECT
            DATE_TRUNC('month', event_date) AS month,
            COUNT(DISTINCT user_id) AS total_users
        FROM user_events
        GROUP BY 1
    )
    SELECT
        f.month, f.feature,
        f.users_using * 100.0 / NULLIF(t.total_users, 0) AS adoption_pct
    FROM feature_users f
    JOIN total t ON f.month = t.month
    ORDER BY 1, 3 DESC
```

- [ ] **Step 4: Create sales metrics**

Create `docs/metrics/sales/new_customers.yml`:

```yaml
- name: new_customers
  display_name: New Customers
  category: sales
  type: count
  unit: count
  grain: monthly
  table: orders
  expression: "COUNT(DISTINCT customer_id)"
  time_column: order_date
  dimensions:
    - channel
    - region
  synonyms:
    - customer_acquisition
    - new_logos
  sql: |
    SELECT
        DATE_TRUNC('month', first_order_date) AS month,
        COUNT(DISTINCT customer_id) AS new_customers
    FROM (
        SELECT customer_id, MIN(order_date) AS first_order_date
        FROM orders
        WHERE status = 'completed'
        GROUP BY 1
    )
    GROUP BY 1
    ORDER BY 1
```

Create `docs/metrics/sales/upsell_expansion.yml`:

```yaml
- name: upsell_expansion
  display_name: Upsell & Expansion Revenue
  category: sales
  type: sum
  unit: USD
  grain: monthly
  table: subscriptions
  expression: "SUM(CASE WHEN change_type IN ('upgrade','expansion') THEN delta_mrr END)"
  time_column: change_date
  synonyms:
    - expansion_revenue
    - upsell
  sql: |
    SELECT
        DATE_TRUNC('month', change_date) AS month,
        SUM(delta_mrr) AS expansion_mrr
    FROM subscription_changes
    WHERE change_type IN ('upgrade', 'expansion')
    GROUP BY 1
    ORDER BY 1
```

Create `docs/metrics/sales/pipeline_value.yml`:

```yaml
- name: pipeline_value
  display_name: Pipeline Value
  category: sales
  type: sum
  unit: USD
  grain: monthly
  table: opportunities
  expression: "SUM(deal_value * probability / 100)"
  time_column: expected_close_date
  dimensions:
    - stage
    - owner
  synonyms:
    - weighted_pipeline
    - deal_pipeline
  sql: |
    SELECT
        DATE_TRUNC('month', expected_close_date) AS month,
        SUM(deal_value * probability / 100) AS weighted_pipeline
    FROM opportunities
    WHERE stage NOT IN ('closed_won', 'closed_lost')
    GROUP BY 1
    ORDER BY 1
```

- [ ] **Step 5: Create operations metrics**

Create `docs/metrics/operations/support_resolution_time.yml`:

```yaml
- name: support_resolution_time
  display_name: Support Resolution Time
  category: operations
  type: avg
  unit: hours
  grain: monthly
  table: tickets
  expression: "AVG(resolution_hours)"
  time_column: created_at
  dimensions:
    - priority
    - category
  synonyms:
    - resolution_time
    - mttr
    - mean_time_to_resolve
  sql: |
    SELECT
        DATE_TRUNC('month', created_at) AS month,
        AVG(resolution_hours) AS avg_resolution_hours,
        PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY resolution_hours) AS median_hours
    FROM tickets
    WHERE status = 'resolved'
    GROUP BY 1
    ORDER BY 1
```

Create `docs/metrics/operations/infrastructure_cost.yml`:

```yaml
- name: infrastructure_cost
  display_name: Infrastructure Cost
  category: operations
  type: sum
  unit: USD
  grain: monthly
  table: infra_costs
  expression: "SUM(cost_usd)"
  time_column: billing_month
  dimensions:
    - provider
    - service
    - environment
  synonyms:
    - cloud_cost
    - hosting_cost
    - infra_spend
  sql: |
    SELECT
        billing_month AS month,
        SUM(cost_usd) AS total_cost
    FROM infra_costs
    GROUP BY 1
    ORDER BY 1
  sql_by_provider: |
    SELECT
        billing_month AS month,
        provider,
        SUM(cost_usd) AS cost
    FROM infra_costs
    GROUP BY 1, 2
    ORDER BY 1, 3 DESC
```

- [ ] **Step 6: Write import test for starter pack**

Add to `tests/test_metrics.py`:

```python
class TestStarterPack:
    def test_import_starter_pack(self, db_conn):
        from src.repositories.metrics import MetricRepository
        repo = MetricRepository(db_conn)
        starter_dir = Path(__file__).parent.parent / "docs" / "metrics"
        if not starter_dir.exists():
            pytest.skip("Starter pack not found")
        count = repo.import_from_yaml(starter_dir)
        assert count >= 11  # 11 metrics (total_revenue + 10 new)
        assert repo.get("revenue/total_revenue") is not None
        assert repo.get("revenue/mrr") is not None
        assert repo.get("operations/infrastructure_cost") is not None
```

- [ ] **Step 7: Run tests**

Run: `pytest tests/test_metrics.py::TestStarterPack -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add docs/metrics/ tests/test_metrics.py
git commit -m "feat: add 10 starter pack metrics (revenue, usage, sales, operations)"
```

---

### Task 7: Profiler Integration

**Files:**
- Modify: `src/profiler.py:1154,1248` (replace load_metrics calls)
- Test: manual verification (profiler is integration-heavy)

- [ ] **Step 1: Add get_table_map fallback to profiler**

In `src/profiler.py`, find the two call sites where `load_metrics()` is called (lines ~1154 and ~1248). In each location, replace:

```python
    metrics_map = load_metrics(METRICS_YML_PATH)
```

with:

```python
    # Try DuckDB-backed metrics first, fall back to YAML scan
    metrics_map = _load_metrics_from_db()
    if not metrics_map:
        metrics_map = load_metrics(METRICS_YML_PATH)
```

Add this helper function near the top of the file (after the imports):

```python
def _load_metrics_from_db() -> Dict[str, List[str]]:
    """Load metrics table map from DuckDB. Returns empty dict on failure."""
    try:
        from src.db import get_system_db
        from src.repositories.metrics import MetricRepository
        conn = get_system_db()
        repo = MetricRepository(conn)
        table_map = repo.get_table_map()
        conn.close()
        return table_map
    except Exception as exc:
        logger.debug("Could not load metrics from DuckDB: %s", exc)
        return {}
```

- [ ] **Step 2: Run existing profiler tests**

Run: `pytest tests/ -k profiler -v`
Expected: ALL PASS (existing tests should not break)

- [ ] **Step 3: Commit**

```bash
git add src/profiler.py
git commit -m "feat: profiler reads metrics from DuckDB with YAML fallback"
```

---

### Task 8: CLAUDE.md Update

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Add metrics workflow section to CLAUDE.md**

After the "## Development" section, add:

```markdown
## Business Metrics

Standardized metric definitions live in DuckDB (`metric_definitions` table). Import starter pack:

```bash
da metrics import docs/metrics/
```

### For AI agents analyzing data:
Before computing any business metric, look up the canonical definition:
1. `da metrics list` — find the relevant metric
2. `da metrics show revenue/mrr` — read the SQL and business rules
3. Use the SQL from the metric definition, adapt to the specific question

Never invent metric calculations — always use the canonical definitions.
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: add metrics workflow instructions to CLAUDE.md"
```

---

### Task 9: Migration Script

**Files:**
- Create: `scripts/migrate_metrics_to_duckdb.py`

- [ ] **Step 1: Create standalone migration script**

Create `scripts/migrate_metrics_to_duckdb.py`:

```python
"""Migrate metric YAML files to DuckDB metric_definitions table.

Usage:
    python scripts/migrate_metrics_to_duckdb.py [--metrics-dir docs/metrics]

Idempotent — safe to run repeatedly. Uses UPSERT (ON CONFLICT DO UPDATE).
"""

import argparse
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Migrate metric YAMLs to DuckDB")
    parser.add_argument("--metrics-dir", default="docs/metrics", help="Path to metrics directory")
    args = parser.parse_args()

    metrics_dir = Path(args.metrics_dir)
    if not metrics_dir.is_dir():
        logger.error("Metrics directory not found: %s", metrics_dir)
        sys.exit(1)

    from src.db import get_system_db
    from src.repositories.metrics import MetricRepository

    conn = get_system_db()
    try:
        repo = MetricRepository(conn)
        count = repo.import_from_yaml(metrics_dir)
        logger.info("Imported %d metrics from %s", count, metrics_dir)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run it manually to verify**

Run: `python scripts/migrate_metrics_to_duckdb.py --metrics-dir docs/metrics`
Expected: `Imported N metrics from docs/metrics`

- [ ] **Step 3: Commit**

```bash
git add scripts/migrate_metrics_to_duckdb.py
git commit -m "feat: add standalone metric YAML → DuckDB migration script"
```

---

### Task 10: Final Integration Test

- [ ] **Step 1: Run full test suite**

Run: `pytest tests/ -v --timeout=60`
Expected: ALL PASS (no regressions)

- [ ] **Step 2: Run OpenAPI snapshot test (if exists)**

Run: `pytest tests/ -k openapi -v`
Expected: May need snapshot update if endpoint list changed. If it fails, regenerate with `python scripts/generate_openapi.py`

- [ ] **Step 3: Final commit if any fixes needed**

```bash
git add -A
git commit -m "fix: address integration test issues"
```
