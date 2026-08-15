"""sync_semantic_layer() for Databricks — fake statement client, real test DuckDB
via the e2e_env fixture (same pattern as tests/test_keboola_semantic_layer_sync.py)."""

from __future__ import annotations

from unittest.mock import patch


from connectors.databricks.client import DatabricksApiError

_SETTINGS = {
    "host": "https://dbc-test.cloud.databricks.com",
    "warehouse_id": "wh-1",
    "catalog": "main",
    "catalogs": ["main"],
    "token": "tok",
}

_YAML = """
version: 1.1
source: SELECT * FROM main.sales.orders
dimensions:
  - name: order_date
    expr: o_orderdate
  - name: country
    expr: c_country
measures:
  - name: Order Count
    expr: COUNT(o_orderkey)
  - name: Total Revenue
    expr: SUM(o_totalprice)
    description: Gross revenue before refunds
"""


class FakeStatementClient:
    """Routes the two query shapes the sync issues: metric-view discovery
    (information_schema) and SHOW CREATE TABLE per view."""

    def __init__(self, views=None, yaml_by_view=None, fail_with=None):
        # views: list of (catalog, schema, name, comment)
        self.views = views if views is not None else [("main", "sales", "orders_metrics", "Sales KPIs")]
        self.yaml_by_view = yaml_by_view or {}
        self.fail_with = fail_with
        self.statements = []

    def execute_rows(self, statement, **_kwargs):
        self.statements.append(statement)
        if self.fail_with is not None:
            raise self.fail_with
        if "information_schema" in statement:
            return (
                ["table_catalog", "table_schema", "table_name", "comment"],
                [list(v) for v in self.views],
            )
        if statement.startswith("SHOW CREATE TABLE"):
            view_name = statement.rsplit(".", 1)[-1].strip("`")
            body = self.yaml_by_view.get(view_name, _YAML)
            if body is None:
                return (["createtab_stmt"], [["CREATE VIEW broken AS SELECT 1"]])
            return (
                ["createtab_stmt"],
                [[f"CREATE VIEW x WITH METRICS\nLANGUAGE YAML\nAS $$\n{body}\n$$"]],
            )
        raise AssertionError(f"unexpected statement: {statement}")


def _sync(client):
    from connectors.databricks.semantic_layer import sync_semantic_layer

    with patch(
        "connectors.databricks.semantic_layer.resolve_databricks_settings",
        return_value=_SETTINGS,
    ):
        return sync_semantic_layer(client=client)


class TestSyncSemanticLayer:
    def test_creates_one_metric_per_measure(self, e2e_env):
        from src.repositories import metric_repo

        result = _sync(FakeStatementClient())
        assert result["status"] == "ok"
        assert result["metric_views_seen"] == 1
        assert result["created_or_updated"] == 2
        assert result["source_ref"] == "dbc-test.cloud.databricks.com"

        row = metric_repo().get("databricks/main.sales.orders_metrics/Total Revenue")
        assert row is not None
        assert row["source"] == "databricks_semantic_layer"
        assert row["source_ref"] == "dbc-test.cloud.databricks.com"
        assert row["sql"] == "SELECT MEASURE(`Total Revenue`) FROM `main`.`sales`.`orders_metrics`"
        assert row["expression"] == "SUM(o_totalprice)"
        assert row["description"] == "Gross revenue before refunds"
        assert list(row["dimensions"]) == ["order_date", "country"]
        assert row["category"] == "databricks"
        # The notes must tell an agent where this runs — MEASURE() is
        # warehouse-only, never local DuckDB.
        assert any("SQL warehouse" in n for n in row["notes"])

    def test_prunes_measures_removed_upstream(self, e2e_env):
        from src.repositories import metric_repo

        _sync(FakeStatementClient())
        assert metric_repo().get("databricks/main.sales.orders_metrics/Order Count") is not None

        one_measure_yaml = "version: 1.1\nsource: t\nmeasures:\n  - name: Total Revenue\n    expr: SUM(x)\n"
        result = _sync(FakeStatementClient(yaml_by_view={"orders_metrics": one_measure_yaml}))
        assert result["pruned"] == 1
        assert metric_repo().get("databricks/main.sales.orders_metrics/Order Count") is None
        assert metric_repo().get("databricks/main.sales.orders_metrics/Total Revenue") is not None

    def test_zero_usable_measures_skips_prune(self, e2e_env):
        from src.repositories import metric_repo

        _sync(FakeStatementClient())
        # Upstream vocabulary drift: discovery finds nothing → prune must NOT
        # wipe the previously-synced rows.
        result = _sync(FakeStatementClient(views=[]))
        assert result["status"] == "ok"
        assert result["pruned"] == 0
        assert metric_repo().get("databricks/main.sales.orders_metrics/Total Revenue") is not None

    def test_never_touches_other_writers_rows(self, e2e_env):
        from src.repositories import metric_repo

        metric_repo().create(
            id="manual/revenue",
            name="Total Revenue",
            display_name="Total Revenue",
            category="finance",
            sql="SELECT SUM(amount) FROM orders",
            source="manual",
        )
        result = _sync(FakeStatementClient())
        # The manual row owns the name — the sync must skip, count the
        # conflict, and leave the manual row byte-for-byte intact.
        assert result["skipped_conflict"] == 1
        assert result["created_or_updated"] == 1  # Order Count still lands
        row = metric_repo().get("manual/revenue")
        assert row["source"] == "manual"
        assert row["sql"] == "SELECT SUM(amount) FROM orders"
        assert metric_repo().get("databricks/main.sales.orders_metrics/Total Revenue") is None

        # And the conflicting id must survive future prunes (retained, not seen).
        result2 = _sync(FakeStatementClient())
        assert result2["pruned"] == 0

    def test_unparseable_view_is_counted_not_fatal(self, e2e_env):
        result = _sync(FakeStatementClient(yaml_by_view={"orders_metrics": None}))
        assert result["status"] == "ok"
        assert result["skipped_unparseable"] == 1
        assert result["created_or_updated"] == 0

    def test_unconfigured_instance_reports_error_code(self, e2e_env):
        from connectors.databricks.semantic_layer import sync_semantic_layer

        with patch(
            "connectors.databricks.semantic_layer.resolve_databricks_settings",
            return_value=None,
        ):
            result = sync_semantic_layer()
        assert result["status"] == "error"
        assert result["code"] == "credentials_not_configured"

    def test_upstream_4xx_maps_to_client_error_code(self, e2e_env):
        result = _sync(FakeStatementClient(fail_with=DatabricksApiError("denied", status=403)))
        assert result["status"] == "error"
        assert result["code"] == "upstream_client_error"

    def test_upstream_5xx_maps_to_upstream_error_code(self, e2e_env):
        result = _sync(FakeStatementClient(fail_with=DatabricksApiError("boom", status=503)))
        assert result["status"] == "error"
        assert result["code"] == "upstream_error"


class TestBuildMetricRows:
    def test_measure_names_with_backticks_are_skipped(self):
        from connectors.databricks.semantic_layer import build_metric_rows

        yaml_text = "measures:\n  - name: 'bad`tick'\n    expr: COUNT(1)\n"
        rows, reason = build_metric_rows("c", "s", "v", "", yaml_text, source_ref="w")
        # Backticks are escaped by doubling in the composed SQL — the row is
        # still produced and the SQL stays inside the quoted identifier.
        assert reason is None
        assert rows[0]["sql"] == "SELECT MEASURE(`bad``tick`) FROM `c`.`s`.`v`"

    def test_non_mapping_yaml_is_skipped(self):
        from connectors.databricks.semantic_layer import build_metric_rows

        rows, reason = build_metric_rows("c", "s", "v", "", "- just\n- a list\n", source_ref="w")
        assert rows == []
        assert reason == "yaml_not_a_mapping"

    def test_no_measures_is_skipped(self):
        from connectors.databricks.semantic_layer import build_metric_rows

        rows, reason = build_metric_rows("c", "s", "v", "", "version: 1.1\nsource: t\n", source_ref="w")
        assert rows == []
        assert reason == "no_measures"
