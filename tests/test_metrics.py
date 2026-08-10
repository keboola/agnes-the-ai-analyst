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
        # 1 metric using tables[] array (no table_name)
        repo.create(
            id="combined/multi",
            name="multi",
            display_name="Multi Table Metric",
            category="combined",
            tables=["a", "b"],
            sql="SELECT 1",
        )
        sub_metrics = repo.find_by_table("subscriptions")
        assert len(sub_metrics) == 2
        ids = {m["id"] for m in sub_metrics}
        assert "revenue/mrr" in ids
        assert "revenue/arr" in ids

        event_metrics = repo.find_by_table("events")
        assert len(event_metrics) == 1
        assert event_metrics[0]["id"] == "engagement/dau"

        # Metric referencing 'a' via tables[] array should be found
        a_metrics = repo.find_by_table("a")
        assert len(a_metrics) == 1
        assert a_metrics[0]["id"] == "combined/multi"

        # Metric referencing 'b' via tables[] array should also be found
        b_metrics = repo.find_by_table("b")
        assert len(b_metrics) == 1
        assert b_metrics[0]["id"] == "combined/multi"

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
        # Metric using tables[] array
        repo.create(
            id="combined/multi",
            name="multi",
            display_name="Multi Table Metric",
            category="combined",
            tables=["subscriptions", "events"],
            sql="SELECT 1",
        )
        table_map = repo.get_table_map()
        assert isinstance(table_map, dict)
        assert "subscriptions" in table_map
        assert "events" in table_map
        # 'subscriptions' should include mrr, arr (table_name) plus multi (tables[])
        assert "mrr" in table_map["subscriptions"]
        assert "arr" in table_map["subscriptions"]
        assert "multi" in table_map["subscriptions"]
        # 'events' should include dau (table_name) plus multi (tables[])
        assert "dau" in table_map["events"]
        assert "multi" in table_map["events"]

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


class TestStarterPack:
    def test_import_starter_pack(self, db_conn):
        from src.repositories.metrics import MetricRepository
        from pathlib import Path
        repo = MetricRepository(db_conn)
        starter_dir = Path(__file__).parent.parent / "docs" / "metrics"
        if not starter_dir.exists():
            pytest.skip("Starter pack not found")
        count = repo.import_from_yaml(starter_dir)
        assert count >= 11  # total_revenue + 10 new
        assert repo.get("revenue/total_revenue") is not None
        assert repo.get("revenue/mrr") is not None
        assert repo.get("operations/infrastructure_cost") is not None


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


class TestMetricRepositoryReconcile:
    """`reconcile_from_yaml` — the import that can also SHRINK the registry.

    `import_from_yaml` is upsert-only, so a metric deleted upstream stays
    forever and a rename leaves both ids behind, indistinguishable from a
    hand-authored metric (#1219). Reconcile adds the missing direction, keyed
    on the writer recorded in `source` (+ optional `source_ref`) so it can
    never reach a metric a human wrote in the UI.
    """

    def _repo(self, db_conn):
        from src.repositories.metrics import MetricRepository

        return MetricRepository(db_conn)

    def test_dry_run_reports_without_writing(self, db_conn, metrics_dir):
        repo = self._repo(db_conn)
        report = repo.reconcile_from_yaml(metrics_dir, dry_run=True)
        assert set(report["added"]) == {"revenue/total_revenue", "operations/resolution_time"}
        assert report["updated"] == [] and report["deleted"] == []
        assert repo.list() == [], "dry run must not write"

    def test_second_run_reports_updates_not_adds(self, db_conn, metrics_dir):
        repo = self._repo(db_conn)
        repo.reconcile_from_yaml(metrics_dir)
        report = repo.reconcile_from_yaml(metrics_dir, dry_run=True)
        assert report["added"] == []
        assert set(report["updated"]) == {"revenue/total_revenue", "operations/resolution_time"}

    def test_prune_removes_a_metric_the_source_dropped(self, db_conn, metrics_dir):
        repo = self._repo(db_conn)
        repo.reconcile_from_yaml(metrics_dir)
        (metrics_dir / "operations" / "resolution_time.yml").unlink()

        report = repo.reconcile_from_yaml(metrics_dir, prune=True)
        assert report["deleted"] == ["operations/resolution_time"]
        assert {m["id"] for m in repo.list()} == {"revenue/total_revenue"}

    def test_without_prune_the_dropped_metric_survives(self, db_conn, metrics_dir):
        """The default stays upsert-only — shrinking is opt-in."""
        repo = self._repo(db_conn)
        repo.reconcile_from_yaml(metrics_dir)
        (metrics_dir / "operations" / "resolution_time.yml").unlink()

        report = repo.reconcile_from_yaml(metrics_dir)
        assert report["deleted"] == []
        assert len(repo.list()) == 2

    def test_prune_never_touches_a_hand_authored_metric(self, db_conn, metrics_dir):
        """The whole reason the scope is keyed on the writer: a metric an
        admin wrote in the UI is not in any directory, and must not be read
        as 'deleted upstream'."""
        repo = self._repo(db_conn)
        repo.create(id="manual/nps", name="nps", display_name="NPS", category="manual", sql="SELECT 1", source="manual")
        repo.reconcile_from_yaml(metrics_dir, prune=True)
        assert repo.get("manual/nps") is not None

    def test_prune_never_touches_another_importers_metric(self, db_conn, metrics_dir):
        repo = self._repo(db_conn)
        repo.create(
            id="keboola/live_deals", name="live_deals", display_name="Live Deals",
            category="keboola", sql="SELECT 1", source="keboola_semantic_layer",
        )
        repo.reconcile_from_yaml(metrics_dir, prune=True)
        assert repo.get("keboola/live_deals") is not None

    def test_source_ref_narrows_the_scope_to_one_export(self, db_conn, metrics_dir, tmp_path):
        """Two YAML exports into one instance both carry source='yaml_import',
        so without a narrower key the second import would delete the first's
        metrics. `--source-ref` is that key."""
        repo = self._repo(db_conn)
        repo.reconcile_from_yaml(metrics_dir, source_ref="finance")

        other = tmp_path / "other" / "growth"
        other.mkdir(parents=True)
        (other / "signups.yml").write_text("name: signups\nsql: SELECT 1\n")
        repo.reconcile_from_yaml(other.parent, source_ref="growth", prune=True)

        ids = {m["id"] for m in repo.list()}
        assert "growth/signups" in ids
        assert "revenue/total_revenue" in ids, "pruning the 'growth' export deleted the 'finance' one"

    def test_source_ref_is_stamped_on_the_rows(self, db_conn, metrics_dir):
        repo = self._repo(db_conn)
        repo.reconcile_from_yaml(metrics_dir, source_ref="finance")
        assert repo.get("revenue/total_revenue")["source_ref"] == "finance"

    def test_import_from_yaml_still_returns_a_count(self, db_conn, metrics_dir):
        """The old entry point keeps its signature — eleven callers rely on it."""
        repo = self._repo(db_conn)
        assert repo.import_from_yaml(metrics_dir) == 2


class TestMetricReconcileRefusesToWipe:
    """`--prune` deletes everything in scope that the input does not mention,
    so an input that mentions almost nothing is indistinguishable from "the
    source dropped almost everything". These are the two shapes where that
    reading is nearly always wrong, and the cost of being wrong is data loss.
    """

    def _repo(self, db_conn):
        from src.repositories.metrics import MetricRepository

        return MetricRepository(db_conn)

    def test_prune_against_a_single_file_is_refused(self, db_conn, metrics_dir):
        """A file describes some metrics; it cannot be the source of truth for
        a whole scope. Before this guard, pointing prune at one file deleted
        every other imported metric."""
        repo = self._repo(db_conn)
        repo.reconcile_from_yaml(metrics_dir)
        with pytest.raises(ValueError, match="single file"):
            repo.reconcile_from_yaml(metrics_dir / "revenue" / "total_revenue.yml", prune=True)
        assert len(repo.list()) == 2, "refused prune must not have deleted anything"

    def test_prune_against_a_directory_that_yields_nothing_is_refused(self, db_conn, metrics_dir, tmp_path):
        """The worst shape: the layout is `<dir>/<category>/<name>.yml`, so a
        directory whose files sit one level too high globs to zero files — and
        an empty input told prune to delete the entire scope."""
        repo = self._repo(db_conn)
        repo.reconcile_from_yaml(metrics_dir)
        flat = tmp_path / "flat"
        flat.mkdir()
        (flat / "mrr.yml").write_text("name: mrr\ncategory: revenue\nsql: SELECT 1\n")

        with pytest.raises(ValueError, match="no metrics"):
            repo.reconcile_from_yaml(flat, prune=True)
        assert len(repo.list()) == 2

    def test_those_inputs_are_still_fine_without_prune(self, db_conn, metrics_dir):
        """The guard is on the destructive combination only — importing a
        single file has always been legitimate."""
        repo = self._repo(db_conn)
        n = repo.import_from_yaml(metrics_dir / "revenue" / "total_revenue.yml")
        assert n == 1

    def test_dry_run_is_refused_too(self, db_conn, metrics_dir):
        """A dry run that reports 'would delete everything' would teach the
        operator the wrong thing about a path that must never be pruned."""
        repo = self._repo(db_conn)
        repo.reconcile_from_yaml(metrics_dir)
        with pytest.raises(ValueError):
            repo.reconcile_from_yaml(metrics_dir / "revenue" / "total_revenue.yml", prune=True, dry_run=True)


class TestMetricReconcileLabelPartitions:
    """Each `--source-ref` owns its own partition, and the unlabeled import
    owns the unlabeled one. Without this, an unlabeled prune deleted labeled
    exports' metrics — the exact coexistence the flag promises."""

    def _repo(self, db_conn):
        from src.repositories.metrics import MetricRepository

        return MetricRepository(db_conn)

    def test_unlabeled_prune_spares_a_labeled_export(self, db_conn, metrics_dir, tmp_path):
        repo = self._repo(db_conn)
        repo.reconcile_from_yaml(metrics_dir, source_ref="finance")

        other = tmp_path / "other" / "growth"
        other.mkdir(parents=True)
        (other / "signups.yml").write_text("name: signups\ncategory: growth\nsql: SELECT 1\n")
        report = repo.reconcile_from_yaml(other.parent, prune=True)

        assert report["deleted"] == []
        ids = {m["id"] for m in repo.list()}
        assert "revenue/total_revenue" in ids, "unlabeled prune deleted a labeled export's metrics"

    def test_labeled_prune_spares_the_unlabeled_rows(self, db_conn, metrics_dir, tmp_path):
        repo = self._repo(db_conn)
        repo.reconcile_from_yaml(metrics_dir)  # unlabeled

        other = tmp_path / "other" / "growth"
        other.mkdir(parents=True)
        (other / "signups.yml").write_text("name: signups\ncategory: growth\nsql: SELECT 1\n")
        report = repo.reconcile_from_yaml(other.parent, source_ref="growth", prune=True)

        assert report["deleted"] == []
        assert len(repo.list()) == 3


class TestMetricReconcileScopeExcludesWebUploads:
    """A metric uploaded through the admin page is a different writer.

    Before this, `POST /api/admin/metrics/import` stamped the same
    `source='yaml_import'` the CLI import uses, so a prune against some
    directory deleted metrics an admin had uploaded by hand — the exact
    outcome the "keyed on the writer" scope exists to prevent.
    """

    def test_prune_spares_a_web_uploaded_metric(self, db_conn, metrics_dir):
        from src.repositories.metrics import MetricRepository

        repo = MetricRepository(db_conn)
        repo.reconcile_from_yaml(metrics_dir)
        repo.create(
            id="ops/uploaded", name="uploaded", display_name="Uploaded", category="ops",
            sql="SELECT 1", source="web_upload",
        )
        (metrics_dir / "operations" / "resolution_time.yml").unlink()

        report = repo.reconcile_from_yaml(metrics_dir, prune=True)
        assert report["deleted"] == ["operations/resolution_time"]
        assert repo.get("ops/uploaded") is not None


class TestMetricReconcileRefusesEmptyParse:
    """`files` existing is not the same as metrics parsing out of them.

    A truncated or half-written export directory has files that yield nothing,
    and an empty parse is how you tell prune "delete the whole scope".
    """

    def test_prune_against_unparseable_files_is_refused(self, db_conn, metrics_dir, tmp_path):
        from src.repositories.metrics import MetricRepository

        repo = MetricRepository(db_conn)
        repo.reconcile_from_yaml(metrics_dir)

        bad = tmp_path / "bad" / "revenue"
        bad.mkdir(parents=True)
        (bad / "empty.yml").write_text("")
        (bad / "junk.yml").write_text("just a string\n")

        # Caught by the per-file guard, which fires first and names the files;
        # the empty-parse guard behind it covers a directory with no files at all.
        with pytest.raises(ValueError, match="could not be read"):
            repo.reconcile_from_yaml(bad.parent, prune=True)
        assert len(repo.list()) == 2, "refused prune must not have deleted anything"

    def test_importing_unparseable_files_without_prune_is_still_a_no_op(self, db_conn, tmp_path):
        from src.repositories.metrics import MetricRepository

        repo = MetricRepository(db_conn)
        bad = tmp_path / "bad" / "revenue"
        bad.mkdir(parents=True)
        (bad / "empty.yml").write_text("")
        assert repo.import_from_yaml(bad.parent) == 0


class TestMetricReconcileReportsAdoption:
    """An incoming id that already belongs to a DIFFERENT writer is neither
    "added" nor a routine "update" — the import overwrites that row and
    re-stamps its `source`, which moves it into prune scope. Reporting it as
    "added" told the operator a new metric would appear while a hand-authored
    one was about to be taken over.
    """

    def _repo(self, db_conn):
        from src.repositories.metrics import MetricRepository

        return MetricRepository(db_conn)

    def _yaml(self, tmp_path, metric_id="revenue/total_revenue"):
        category, name = metric_id.split("/")
        d = tmp_path / "in" / category
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{name}.yml").write_text(f"name: {name}\ncategory: {category}\nsql: SELECT 999\n")
        return tmp_path / "in"

    def test_taking_over_a_hand_authored_metric_is_reported_as_adopted(self, db_conn, tmp_path):
        repo = self._repo(db_conn)
        repo.create(
            id="revenue/total_revenue", name="total_revenue", display_name="Hand written",
            category="revenue", sql="SELECT 1", source="manual",
        )
        report = repo.reconcile_from_yaml(self._yaml(tmp_path), dry_run=True)
        assert report["adopted"] == ["revenue/total_revenue"]
        assert report["added"] == [], "an existing row is not an addition"

    def test_a_genuinely_new_metric_is_still_added(self, db_conn, tmp_path):
        repo = self._repo(db_conn)
        report = repo.reconcile_from_yaml(self._yaml(tmp_path), dry_run=True)
        assert report["added"] == ["revenue/total_revenue"]
        assert report["adopted"] == []

    def test_reimporting_our_own_metric_is_an_update_not_an_adoption(self, db_conn, tmp_path):
        repo = self._repo(db_conn)
        src = self._yaml(tmp_path)
        repo.reconcile_from_yaml(src)
        report = repo.reconcile_from_yaml(src, dry_run=True)
        assert report["updated"] == ["revenue/total_revenue"]
        assert report["adopted"] == []

    def test_a_metric_from_another_label_counts_as_adopted(self, db_conn, tmp_path):
        repo = self._repo(db_conn)
        src = self._yaml(tmp_path)
        repo.reconcile_from_yaml(src, source_ref="finance")
        report = repo.reconcile_from_yaml(src, source_ref="growth", dry_run=True)
        assert report["adopted"] == ["revenue/total_revenue"]


class TestMetricReconcileAuditsBeforeDeleting:
    """The audit row must be written BEFORE the delete, not after it.

    An interruption between the two otherwise leaves a metric deleted with no
    record — on the only destructive path this repo has.
    """

    def test_the_callback_fires_before_the_row_is_gone(self, db_conn, metrics_dir):
        from src.repositories.metrics import MetricRepository

        repo = MetricRepository(db_conn)
        repo.reconcile_from_yaml(metrics_dir)
        (metrics_dir / "operations" / "resolution_time.yml").unlink()

        seen = []
        repo.reconcile_from_yaml(
            metrics_dir,
            prune=True,
            on_delete=lambda mid: seen.append((mid, repo.get(mid) is not None)),
        )
        assert seen == [("operations/resolution_time", True)], "callback ran after the delete"

    def test_no_callback_fires_on_a_dry_run(self, db_conn, metrics_dir):
        from src.repositories.metrics import MetricRepository

        repo = MetricRepository(db_conn)
        repo.reconcile_from_yaml(metrics_dir)
        (metrics_dir / "operations" / "resolution_time.yml").unlink()

        seen = []
        repo.reconcile_from_yaml(metrics_dir, prune=True, dry_run=True, on_delete=seen.append)
        assert seen == []


class TestMetricReconcileRefusesPartialParse:
    """A half-written export is the dangerous middle case.

    All-or-nothing shapes were already refused, but a directory where *some*
    files are truncated still pruned: those metrics silently failed to parse,
    and prune cannot tell "the file is broken" from "the source dropped it".
    """

    def _repo(self, db_conn):
        from src.repositories.metrics import MetricRepository

        return MetricRepository(db_conn)

    def test_prune_is_refused_when_a_file_yields_no_metric(self, db_conn, metrics_dir):
        repo = self._repo(db_conn)
        repo.reconcile_from_yaml(metrics_dir)
        # one good file survives, one is truncated mid-write
        (metrics_dir / "operations" / "resolution_time.yml").write_text("just a truncated string\n")

        with pytest.raises(ValueError, match="could not be read"):
            repo.reconcile_from_yaml(metrics_dir, prune=True)
        assert len(repo.list()) == 2, "refused prune must not have deleted anything"

    def test_the_error_names_the_offending_file(self, db_conn, metrics_dir):
        repo = self._repo(db_conn)
        (metrics_dir / "operations" / "resolution_time.yml").write_text("name:\n")  # no usable name
        with pytest.raises(ValueError, match="resolution_time.yml"):
            repo.reconcile_from_yaml(metrics_dir, prune=True)

    def test_a_broken_file_without_prune_is_skipped_as_before(self, db_conn, metrics_dir):
        """Unchanged for the non-destructive path — import has always skipped
        what it cannot read."""
        repo = self._repo(db_conn)
        (metrics_dir / "operations" / "resolution_time.yml").write_text("just a string\n")
        assert repo.import_from_yaml(metrics_dir) == 1


class TestMetricReconcileRefusalIsAtomic:
    """A refused run must leave the registry exactly as it found it.

    The guards fire on the *shape of the input*, which is knowable before a
    single row is written — so a run that ends in "fix these files first" has
    no business having already applied the readable half.
    """

    def test_a_refused_prune_writes_nothing_at_all(self, db_conn, metrics_dir):
        from src.repositories.metrics import MetricRepository

        repo = MetricRepository(db_conn)
        # one good file, one truncated: the good one must NOT land
        (metrics_dir / "operations" / "resolution_time.yml").write_text("truncated\n")

        with pytest.raises(ValueError):
            repo.reconcile_from_yaml(metrics_dir, prune=True)
        assert repo.list() == [], "a refused run half-applied the import"

    def test_a_refused_prune_does_not_update_existing_rows(self, db_conn, metrics_dir):
        from src.repositories.metrics import MetricRepository

        repo = MetricRepository(db_conn)
        repo.reconcile_from_yaml(metrics_dir)
        before = repo.get("revenue/total_revenue")["sql"]

        (metrics_dir / "revenue" / "total_revenue.yml").write_text(
            "name: total_revenue\ncategory: revenue\nsql: SELECT 'CHANGED'\n"
        )
        (metrics_dir / "operations" / "resolution_time.yml").write_text("truncated\n")

        with pytest.raises(ValueError):
            repo.reconcile_from_yaml(metrics_dir, prune=True)
        assert repo.get("revenue/total_revenue")["sql"] == before
