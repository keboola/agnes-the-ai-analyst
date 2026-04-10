"""Tests for MetricRepository (metric_definitions table)."""

import pytest


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
        assert result is not None
        assert result["id"] == "revenue/mrr"
        assert result["name"] == "mrr"
        assert result["display_name"] == "Monthly Recurring Revenue"
        assert result["category"] == "revenue"
        assert result["description"] == "Total MRR from all subscriptions"
        assert result["type"] == "sum"
        assert result["unit"] == "USD"
        assert result["grain"] == "monthly"
        assert result["table_name"] == "subscriptions"
        assert result["expression"] == "SUM(mrr_amount)"
        assert result["time_column"] == "billing_date"
        assert result["dimensions"] == ["plan_type", "region"]
        assert result["synonyms"] == ["monthly_revenue", "recurring_revenue"]
        assert result["notes"] == ["Excludes one-time fees"]
        assert "SELECT" in result["sql"]
        assert result["source"] == "manual"

    def test_create_duplicate_upserts(self, db_conn):
        from src.repositories.metrics import MetricRepository
        repo = MetricRepository(db_conn)
        repo.create(**SAMPLE_METRIC)
        # Create again with different display_name
        updated = {**SAMPLE_METRIC, "display_name": "MRR (Updated)"}
        repo.create(**updated)
        # Should only have one record
        all_metrics = repo.list()
        assert len(all_metrics) == 1
        assert all_metrics[0]["display_name"] == "MRR (Updated)"

    def test_create_with_defaults(self, db_conn):
        from src.repositories.metrics import MetricRepository
        repo = MetricRepository(db_conn)
        result = repo.create(
            id="test/metric",
            name="test_metric",
            display_name="Test Metric",
            category="test",
            sql="SELECT 1",
        )
        assert result["type"] == "sum"
        assert result["grain"] == "monthly"
        assert result["source"] == "manual"

    def test_create_with_json_fields(self, db_conn):
        from src.repositories.metrics import MetricRepository
        repo = MetricRepository(db_conn)
        result = repo.create(
            **SAMPLE_METRIC,
            sql_variants={"weekly": "SELECT DATE_TRUNC('week', billing_date), SUM(mrr) FROM subscriptions GROUP BY 1"},
            validation={"min": 0, "max": 1000000},
        )
        assert result is not None
        assert result["id"] == "revenue/mrr"


class TestMetricRepositoryRead:
    def test_get_existing(self, db_conn):
        from src.repositories.metrics import MetricRepository
        repo = MetricRepository(db_conn)
        repo.create(**SAMPLE_METRIC)
        metric = repo.get("revenue/mrr")
        assert metric is not None
        assert metric["name"] == "mrr"
        assert metric["category"] == "revenue"

    def test_get_missing(self, db_conn):
        from src.repositories.metrics import MetricRepository
        repo = MetricRepository(db_conn)
        result = repo.get("nonexistent/metric")
        assert result is None

    def test_list_all(self, db_conn):
        from src.repositories.metrics import MetricRepository
        repo = MetricRepository(db_conn)
        repo.create(**SAMPLE_METRIC)
        repo.create(
            id="engagement/dau",
            name="dau",
            display_name="Daily Active Users",
            category="engagement",
            sql="SELECT COUNT(DISTINCT user_id) FROM events WHERE DATE(created_at) = CURRENT_DATE",
        )
        all_metrics = repo.list()
        assert len(all_metrics) == 2
        ids = {m["id"] for m in all_metrics}
        assert "revenue/mrr" in ids
        assert "engagement/dau" in ids

    def test_list_by_category(self, db_conn):
        from src.repositories.metrics import MetricRepository
        repo = MetricRepository(db_conn)
        repo.create(**SAMPLE_METRIC)
        repo.create(
            id="engagement/dau",
            name="dau",
            display_name="Daily Active Users",
            category="engagement",
            sql="SELECT COUNT(DISTINCT user_id) FROM events",
        )
        revenue_metrics = repo.list(category="revenue")
        assert len(revenue_metrics) == 1
        assert revenue_metrics[0]["id"] == "revenue/mrr"

        engagement_metrics = repo.list(category="engagement")
        assert len(engagement_metrics) == 1
        assert engagement_metrics[0]["id"] == "engagement/dau"


class TestMetricRepositoryUpdate:
    def test_update_fields(self, db_conn):
        from src.repositories.metrics import MetricRepository
        repo = MetricRepository(db_conn)
        repo.create(**SAMPLE_METRIC)
        updated = repo.update("revenue/mrr", display_name="MRR (New)", unit="EUR")
        assert updated is not None
        assert updated["display_name"] == "MRR (New)"
        assert updated["unit"] == "EUR"
        # Unchanged fields should persist
        assert updated["name"] == "mrr"
        assert updated["category"] == "revenue"
        assert updated["description"] == "Total MRR from all subscriptions"

    def test_update_missing_returns_none(self, db_conn):
        from src.repositories.metrics import MetricRepository
        repo = MetricRepository(db_conn)
        result = repo.update("nonexistent/metric", display_name="Doesn't matter")
        assert result is None

    def test_update_persists_to_db(self, db_conn):
        from src.repositories.metrics import MetricRepository
        repo = MetricRepository(db_conn)
        repo.create(**SAMPLE_METRIC)
        repo.update("revenue/mrr", unit="GBP")
        # Re-fetch from DB to verify persistence
        metric = repo.get("revenue/mrr")
        assert metric["unit"] == "GBP"


class TestMetricRepositoryDelete:
    def test_delete_existing(self, db_conn):
        from src.repositories.metrics import MetricRepository
        repo = MetricRepository(db_conn)
        repo.create(**SAMPLE_METRIC)
        result = repo.delete("revenue/mrr")
        assert result is True
        assert repo.get("revenue/mrr") is None

    def test_delete_missing(self, db_conn):
        from src.repositories.metrics import MetricRepository
        repo = MetricRepository(db_conn)
        result = repo.delete("nonexistent/metric")
        assert result is False

    def test_delete_only_target(self, db_conn):
        from src.repositories.metrics import MetricRepository
        repo = MetricRepository(db_conn)
        repo.create(**SAMPLE_METRIC)
        repo.create(
            id="engagement/dau",
            name="dau",
            display_name="Daily Active Users",
            category="engagement",
            sql="SELECT 1",
        )
        repo.delete("revenue/mrr")
        all_metrics = repo.list()
        assert len(all_metrics) == 1
        assert all_metrics[0]["id"] == "engagement/dau"


class TestMetricRepositorySearch:
    def test_find_by_table(self, db_conn):
        from src.repositories.metrics import MetricRepository
        repo = MetricRepository(db_conn)
        # 2 metrics with table_name='subscriptions'
        repo.create(**SAMPLE_METRIC)
        repo.create(
            id="revenue/arr",
            name="arr",
            display_name="Annual Recurring Revenue",
            category="revenue",
            table_name="subscriptions",
            sql="SELECT SUM(mrr_amount) * 12 AS arr FROM subscriptions",
        )
        # 1 metric with different table
        repo.create(
            id="engagement/dau",
            name="dau",
            display_name="Daily Active Users",
            category="engagement",
            table_name="events",
            sql="SELECT COUNT(DISTINCT user_id) FROM events",
        )
        sub_metrics = repo.find_by_table("subscriptions")
        assert len(sub_metrics) == 2
        ids = {m["id"] for m in sub_metrics}
        assert "revenue/mrr" in ids
        assert "revenue/arr" in ids

        event_metrics = repo.find_by_table("events")
        assert len(event_metrics) == 1
        assert event_metrics[0]["id"] == "engagement/dau"

    def test_find_by_synonym(self, db_conn):
        from src.repositories.metrics import MetricRepository
        repo = MetricRepository(db_conn)
        repo.create(**SAMPLE_METRIC)  # has synonyms: ["monthly_revenue", "recurring_revenue"]
        repo.create(
            id="engagement/dau",
            name="dau",
            display_name="Daily Active Users",
            category="engagement",
            synonyms=["active_users", "daily_users"],
            sql="SELECT COUNT(DISTINCT user_id) FROM events",
        )
        results = repo.find_by_synonym("monthly_revenue")
        assert len(results) == 1
        assert results[0]["id"] == "revenue/mrr"

        results2 = repo.find_by_synonym("active_users")
        assert len(results2) == 1
        assert results2[0]["id"] == "engagement/dau"

        results3 = repo.find_by_synonym("nonexistent_synonym")
        assert len(results3) == 0

    def test_get_table_map(self, db_conn):
        from src.repositories.metrics import MetricRepository
        repo = MetricRepository(db_conn)
        repo.create(**SAMPLE_METRIC)  # table_name='subscriptions'
        repo.create(
            id="revenue/arr",
            name="arr",
            display_name="Annual Recurring Revenue",
            category="revenue",
            table_name="subscriptions",
            sql="SELECT SUM(mrr_amount) * 12 FROM subscriptions",
        )
        repo.create(
            id="engagement/dau",
            name="dau",
            display_name="Daily Active Users",
            category="engagement",
            table_name="events",
            sql="SELECT COUNT(DISTINCT user_id) FROM events",
        )
        table_map = repo.get_table_map()
        assert isinstance(table_map, dict)
        assert "subscriptions" in table_map
        assert "events" in table_map
        assert set(table_map["subscriptions"]) == {"mrr", "arr"}
        assert table_map["events"] == ["dau"]

    def test_get_table_map_excludes_null_table(self, db_conn):
        from src.repositories.metrics import MetricRepository
        repo = MetricRepository(db_conn)
        # Metric without table_name
        repo.create(
            id="test/no_table",
            name="no_table",
            display_name="No Table Metric",
            category="test",
            sql="SELECT 1",
        )
        table_map = repo.get_table_map()
        assert "None" not in table_map
        assert None not in table_map


@pytest.fixture
def metrics_dir(tmp_path):
    revenue_dir = tmp_path / "metrics" / "revenue"
    revenue_dir.mkdir(parents=True)
    ops_dir = tmp_path / "metrics" / "operations"
    ops_dir.mkdir(parents=True)

    # total_revenue.yml — list-wrapped format with table key and sql_by_channel variant
    (revenue_dir / "total_revenue.yml").write_text(
        "- name: total_revenue\n"
        "  display_name: Total Revenue\n"
        "  category: revenue\n"
        "  type: sum\n"
        "  unit: USD\n"
        "  grain: monthly\n"
        "  table: orders\n"
        "  sql: |\n"
        "    SELECT DATE_TRUNC('month', order_date) AS month, SUM(total_amount) AS revenue FROM orders GROUP BY 1\n"
        "  sql_by_channel: |\n"
        "    SELECT channel, SUM(total_amount) AS revenue FROM orders GROUP BY 1\n"
    )

    # resolution_time.yml — plain dict format (no list wrapper)
    (ops_dir / "resolution_time.yml").write_text(
        "name: resolution_time\n"
        "display_name: Resolution Time\n"
        "type: avg\n"
        "unit: hours\n"
        "grain: weekly\n"
        "table: tickets\n"
        "sql: |\n"
        "  SELECT AVG(resolution_hours) FROM tickets\n"
    )

    return tmp_path / "metrics"


class TestMetricRepositoryImport:
    def test_import_from_directory(self, db_conn, metrics_dir):
        from src.repositories.metrics import MetricRepository
        repo = MetricRepository(db_conn)
        count = repo.import_from_yaml(metrics_dir)
        assert count == 2
        all_metrics = repo.list()
        assert len(all_metrics) == 2
        ids = {m["id"] for m in all_metrics}
        assert "revenue/total_revenue" in ids
        assert "operations/resolution_time" in ids

    def test_import_maps_table_to_table_name(self, db_conn, metrics_dir):
        from src.repositories.metrics import MetricRepository
        repo = MetricRepository(db_conn)
        repo.import_from_yaml(metrics_dir)
        metric = repo.get("revenue/total_revenue")
        assert metric is not None
        assert metric["table_name"] == "orders"

    def test_import_collects_sql_variants(self, db_conn, metrics_dir):
        from src.repositories.metrics import MetricRepository
        import json
        repo = MetricRepository(db_conn)
        repo.import_from_yaml(metrics_dir)
        metric = repo.get("revenue/total_revenue")
        assert metric is not None
        sql_variants = metric["sql_variants"]
        # DuckDB may return as a string — parse if so
        if isinstance(sql_variants, str):
            sql_variants = json.loads(sql_variants)
        assert isinstance(sql_variants, dict)
        assert "by_channel" in sql_variants
        assert "channel" in sql_variants["by_channel"]

    def test_import_single_file(self, db_conn, metrics_dir):
        from src.repositories.metrics import MetricRepository
        repo = MetricRepository(db_conn)
        single_file = metrics_dir / "revenue" / "total_revenue.yml"
        count = repo.import_from_yaml(single_file)
        assert count == 1
        metric = repo.get("revenue/total_revenue")
        assert metric is not None

    def test_import_idempotent(self, db_conn, metrics_dir):
        from src.repositories.metrics import MetricRepository
        repo = MetricRepository(db_conn)
        repo.import_from_yaml(metrics_dir)
        repo.import_from_yaml(metrics_dir)
        all_metrics = repo.list()
        assert len(all_metrics) == 2


class TestMetricRepositoryExport:
    def test_export_to_yaml(self, db_conn, metrics_dir, tmp_path):
        from src.repositories.metrics import MetricRepository
        import yaml
        repo = MetricRepository(db_conn)
        repo.import_from_yaml(metrics_dir)
        output_dir = tmp_path / "exported"
        count = repo.export_to_yaml(output_dir)
        assert count == 2
        # Check expected files exist
        revenue_file = output_dir / "revenue" / "total_revenue.yml"
        ops_file = output_dir / "operations" / "resolution_time.yml"
        assert revenue_file.exists()
        assert ops_file.exists()
        # Verify content uses 'table' not 'table_name'
        with open(revenue_file) as f:
            data = yaml.safe_load(f)
        assert "table" in data
        assert "table_name" not in data
        assert data["table"] == "orders"
        # Verify sql_variants are expanded back to sql_by_* keys
        assert "sql_by_channel" in data
        assert "sql_variants" not in data
