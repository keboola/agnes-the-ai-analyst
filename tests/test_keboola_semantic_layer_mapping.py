"""Pure-function mapping/validation logic for the Keboola semantic-layer
importer (connectors/keboola/semantic_layer.py). No live API calls."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from connectors.keboola.semantic_layer import (
    MasterTokenRequiredError,
    build_metric_row,
    compose_sql,
    dataset_lookup_by_table_id,
    has_embedded_sql_comment,
    merge_constraints,
    references_foreign_alias,
    require_master_token,
    resolve_table_name,
    table_lookup_from_registry,
)


class TestRequireMasterToken:
    def test_passes_silently_for_master_token(self):
        storage_client = MagicMock()
        storage_client.verify_token.return_value = {"isMasterToken": True}

        require_master_token(storage_client)  # must not raise

    def test_raises_for_non_master_token(self):
        storage_client = MagicMock()
        storage_client.verify_token.return_value = {"isMasterToken": False}

        with pytest.raises(MasterTokenRequiredError):
            require_master_token(storage_client)

    def test_raises_for_missing_field(self):
        storage_client = MagicMock()
        storage_client.verify_token.return_value = {}

        with pytest.raises(MasterTokenRequiredError):
            require_master_token(storage_client)


class TestTableLookupFromRegistry:
    def test_builds_bucket_source_table_to_name_map(self):
        rows = [
            {
                "bucket": "in.c-example_source",
                "source_table": "orders",
                "name": "crm_orders",
            },
            {
                "bucket": "in.c-example_source",
                "source_table": "contacts",
                "name": "crm_contacts",
            },
        ]
        lookup = table_lookup_from_registry(rows)
        assert lookup == {
            ("in.c-example_source", "orders"): "crm_orders",
            ("in.c-example_source", "contacts"): "crm_contacts",
        }

    def test_skips_rows_missing_bucket_or_source_table(self):
        rows = [
            {"bucket": None, "source_table": "orders", "name": "x"},
            {"bucket": "in.c-example_source", "source_table": None, "name": "y"},
            {"bucket": "in.c-example_source", "source_table": "contacts", "name": None},
        ]
        assert table_lookup_from_registry(rows) == {}


class TestResolveTableName:
    def test_splits_on_last_dot_bucket_may_contain_dots(self):
        # Bucket ids look like `in.c-example_source` (contain dots themselves) —
        # must split the tableId on the LAST dot, not the first.
        lookup = {("in.c-example_source", "orders"): "crm_orders"}
        assert resolve_table_name("in.c-example_source.orders", lookup) == "crm_orders"

    def test_returns_none_for_unregistered_table(self):
        lookup = {("in.c-example_source", "orders"): "crm_orders"}
        assert resolve_table_name("in.c-example_source.unknown_table", lookup) is None

    def test_returns_none_for_malformed_table_id(self):
        assert resolve_table_name("no_dot_here", {}) is None


class TestDatasetLookupByTableId:
    def test_builds_table_id_to_attributes_map(self):
        items = [
            {
                "type": "semantic-dataset",
                "id": "d1",
                "attributes": {
                    "tableId": "in.c-example_source.orders",
                    "grain": "One row per order",
                },
            },
        ]
        lookup = dataset_lookup_by_table_id(items)
        assert lookup == {
            "in.c-example_source.orders": {
                "tableId": "in.c-example_source.orders",
                "grain": "One row per order",
            }
        }

    def test_skips_items_missing_table_id(self):
        items = [{"type": "semantic-dataset", "id": "d1", "attributes": {"name": "no tableId"}}]
        assert dataset_lookup_by_table_id(items) == {}


class TestReferencesForeignAlias:
    def test_bare_column_reference_is_not_foreign(self):
        assert references_foreign_alias('SUM("cost_value")') is False

    def test_case_expression_without_alias_is_not_foreign(self):
        assert references_foreign_alias("COUNT(CASE WHEN \"status\" = 'error' THEN 1 END)") is False

    def test_alias_qualified_column_is_foreign(self):
        assert references_foreign_alias('ROUND(SUM(TRY_CAST(o."amount" AS DECIMAL(18,2))), 2)') is True

    def test_multiple_foreign_aliases_detected(self):
        assert references_foreign_alias("CASE WHEN um.metric_id = 'x' THEN SUM(kumv.value) ELSE 0 END") is True

    def test_dotted_string_literal_is_not_foreign(self):
        # A dotted value inside a single-quoted literal is data, not an alias
        # reference — must not flag a valid single-table metric as foreign.
        assert references_foreign_alias("COUNT(CASE WHEN \"status\" = 'in.progress' THEN 1 END)") is False
        assert (
            references_foreign_alias("SUM(CASE WHEN \"type\" IN ('order.created', 'payment.failed') THEN 1 END)")
            is False
        )

    def test_dotted_literal_plus_real_alias_still_foreign(self):
        # Masking literals must not hide a genuine alias elsewhere in the expr.
        assert references_foreign_alias('CASE WHEN "s" = \'in.progress\' THEN o."amount" END') is True


class TestComposeSql:
    def test_composes_select_with_alias_t(self):
        assert compose_sql('SUM("amount")', "orders") == 'SELECT SUM("amount") FROM "orders" AS t'


class TestMergeConstraints:
    def test_returns_none_when_no_constraint_references_metric(self):
        constraints = [
            {
                "type": "semantic-constraint",
                "id": "c1",
                "attributes": {
                    "name": "positive",
                    "constraintType": "inequality",
                    "rule": "value >= 0",
                    "metrics": ["other_metric"],
                    "severity": "warning",
                },
            },
        ]
        assert merge_constraints("revenue", constraints) is None

    def test_merges_single_matching_constraint(self):
        constraints = [
            {
                "type": "semantic-constraint",
                "id": "c1",
                "attributes": {
                    "name": "revenue_non_negative",
                    "constraintType": "inequality",
                    "rule": "value >= 0",
                    "metrics": ["revenue"],
                    "severity": "warning",
                },
            },
        ]
        result = merge_constraints("revenue", constraints)
        assert result == {
            "rules": [
                {
                    "name": "revenue_non_negative",
                    "constraint_type": "inequality",
                    "rule": "value >= 0",
                    "severity": "warning",
                },
            ]
        }

    def test_merges_multiple_matching_constraints(self):
        constraints = [
            {
                "type": "semantic-constraint",
                "id": "c1",
                "attributes": {
                    "name": "revenue_non_negative",
                    "constraintType": "inequality",
                    "rule": "value >= 0",
                    "metrics": ["revenue"],
                    "severity": "warning",
                },
            },
            {
                "type": "semantic-constraint",
                "id": "c2",
                "attributes": {
                    "name": "revenue_not_null",
                    "constraintType": "equality",
                    "rule": "value IS NOT NULL",
                    "metrics": ["revenue", "other"],
                    "severity": "critical",
                },
            },
        ]
        result = merge_constraints("revenue", constraints)
        assert len(result["rules"]) == 2
        assert result["rules"][1]["name"] == "revenue_not_null"


def _metric_item(name, sql, dataset, description="", model_uuid="model-1"):
    return {
        "type": "semantic-metric",
        "id": f"id-{name}",
        "attributes": {
            "name": name,
            "sql": sql,
            "dataset": dataset,
            "description": description,
            "modelUUID": model_uuid,
        },
    }


class TestBuildMetricRow:
    def test_builds_row_for_simple_metric(self):
        table_lookup = {("in.c-example_source", "orders"): "crm_orders"}
        dataset_lookup = {}
        metric = _metric_item(
            "total_revenue", 'SUM("amount")', "in.c-example_source.orders", description="Total revenue"
        )

        row, skip_reason = build_metric_row(metric, table_lookup, dataset_lookup, [], "model-1")

        assert skip_reason is None
        assert row["id"] == "keboola/model-1/total_revenue"
        assert row["name"] == "total_revenue"
        assert row["table_name"] == "crm_orders"
        assert row["expression"] == 'SUM("amount")'
        assert row["sql"] == 'SELECT SUM("amount") FROM "crm_orders" AS t'
        assert row["description"] == "Total revenue"
        assert row["source"] == "keboola_semantic_layer"
        assert "validation" not in row

    def test_skips_unresolved_table(self):
        metric = _metric_item("m", 'SUM("x")', "in.c-unknown.table")

        row, skip_reason = build_metric_row(metric, {}, {}, [], "model-1")

        assert row is None
        assert skip_reason == "unresolved_table"

    def test_skips_foreign_alias_expression(self):
        table_lookup = {("in.c-example_source", "orders"): "crm_orders"}
        metric = _metric_item("m", 'SUM(o."amount")', "in.c-example_source.orders")

        row, skip_reason = build_metric_row(metric, table_lookup, {}, [], "model-1")

        assert row is None
        assert skip_reason == "foreign_alias_reference"

    def test_skips_metric_with_missing_name(self):
        # A missing/empty name would stringify to "keboola/model-1/None" and
        # write name=None into metric_repo — guard skips it instead.
        metric = _metric_item(None, 'SUM("x")', "in.c-example_source.orders")

        row, skip_reason = build_metric_row(metric, {}, {}, [], "model-1")

        assert row is None
        assert skip_reason == "missing_name"

    def test_enriches_from_dataset_grain_and_ai_block(self):
        table_lookup = {("in.c-example_source", "orders"): "crm_orders"}
        dataset_lookup = {
            "in.c-example_source.orders": {
                "tableId": "in.c-example_source.orders",
                "grain": "One row per order",
                "primaryKey": ["order_id"],
                "ai": {
                    "synonyms": ["sales"],
                    "hints": ["Join via customer_id"],
                    "warnings": ["Excludes refunds"],
                },
            }
        }
        metric = _metric_item("m", 'SUM("amount")', "in.c-example_source.orders")

        row, skip_reason = build_metric_row(metric, table_lookup, dataset_lookup, [], "model-1")

        assert skip_reason is None
        assert row["grain"] == "One row per order"
        assert row["dimensions"] == ["order_id"]
        assert row["synonyms"] == ["sales"]
        assert row["notes"] == ["Join via customer_id", "Excludes refunds"]

    def test_includes_validation_when_constraint_matches(self):
        table_lookup = {("in.c-example_source", "orders"): "crm_orders"}
        constraints = [
            {
                "type": "semantic-constraint",
                "id": "c1",
                "attributes": {
                    "name": "m_non_negative",
                    "constraintType": "inequality",
                    "rule": "value >= 0",
                    "metrics": ["m"],
                    "severity": "warning",
                },
            },
        ]
        metric = _metric_item("m", 'SUM("amount")', "in.c-example_source.orders")

        row, skip_reason = build_metric_row(metric, table_lookup, {}, constraints, "model-1")

        assert skip_reason is None
        assert row["validation"]["rules"][0]["name"] == "m_non_negative"


class TestHasEmbeddedSqlComment:
    def test_bare_expression_has_no_comment(self):
        assert has_embedded_sql_comment('SUM("amount")') is False

    def test_trailing_comment_referencing_missing_table_detected(self):
        # Verified live (2026-07-15): a real Keboola metric used a trailing
        # `--` comment to note the metric conceptually needs a table not
        # present in the project. Naively appending `FROM ... AS t` after
        # this gets swallowed into the comment, breaking the composed SQL.
        assert (
            has_embedded_sql_comment(
                "ROUND(\"value\" * 100, 2) -- FROM other_table WHERE kpi = 'x' (table not in this project)"
            )
            is True
        )

    def test_trailing_comment_noting_missing_filter_detected(self):
        assert has_embedded_sql_comment("ROUND(SUM(\"delta\") * 12, 2) -- WHERE action IN ('a', 'b') AND YTD") is True

    def test_double_hyphen_inside_single_quoted_literal_is_not_a_comment(self):
        assert has_embedded_sql_comment("SUM(CASE WHEN \"status\" = 'in--progress' THEN 1 END)") is False

    def test_double_hyphen_inside_double_quoted_identifier_is_not_a_comment(self):
        assert has_embedded_sql_comment('SUM("weird--column")') is False


class TestBuildMetricRowSkipsEmbeddedComment:
    def test_skips_metric_with_embedded_comment(self):
        table_lookup = {("in.c-example_source", "orders"): "crm_orders"}
        metric = _metric_item(
            "m", 'ROUND("value" * 100, 2) -- FROM other_table (table not in this project)', "in.c-example_source.orders"
        )

        row, skip_reason = build_metric_row(metric, table_lookup, {}, [], "model-1")

        assert row is None
        assert skip_reason == "embedded_sql_comment"

    def test_embedded_comment_checked_even_when_table_would_resolve(self):
        # Table resolution succeeding must not short-circuit the comment
        # check — a metric with both issues must still be skipped for the
        # comment, not silently composed just because its table is known.
        table_lookup = {("in.c-example_source", "orders"): "crm_orders"}
        metric = _metric_item("m", 'SUM("amount") -- note to self', "in.c-example_source.orders")

        row, skip_reason = build_metric_row(metric, table_lookup, {}, [], "model-1")

        assert row is None
        assert skip_reason == "embedded_sql_comment"
