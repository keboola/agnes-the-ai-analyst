"""Pure-function mapping/validation logic for the Keboola semantic-layer
importer (connectors/keboola/semantic_layer.py). No live API calls."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from connectors.keboola.semantic_layer import (
    MasterTokenRequiredError,
    assign_glossary_id,
    build_glossary_row,
    build_metric_row,
    compose_join_sql,
    compose_sql,
    dataset_lookup_by_table_id,
    extract_foreign_aliases,
    has_embedded_sql_comment,
    merge_constraints,
    parse_on_clause,
    references_foreign_alias,
    relationship_lookup_by_dataset,
    require_master_token,
    resolve_join_aliases,
    resolve_relationship,
    resolve_table_name,
    slugify_term,
    table_lookup_from_registry,
    try_join_composition,
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

    def test_dotted_column_name_in_quoted_identifier_is_not_foreign(self):
        # A dot inside a quoted identifier ("total.amount") is part of the
        # column name, not an <alias>. qualifier — must not be skipped.
        assert references_foreign_alias('SUM("total.amount")') is False


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

    def test_quote_in_identifier_does_not_expose_literal_double_hyphen(self):
        # Masking order: a single quote inside an identifier ("col'name") must
        # not start a spurious string-literal match that re-exposes a `--`
        # safely inside a following real string literal.
        assert has_embedded_sql_comment("SUM(\"col'name\", 'value--here')") is False


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


def _relationship_item(name, from_id, to_id, on, rel_type="left", model_uuid="model-1"):
    return {
        "type": "semantic-relationship",
        "id": f"id-{name}",
        "attributes": {
            "name": name,
            "from": from_id,
            "to": to_id,
            "on": on,
            "type": rel_type,
            "modelUUID": model_uuid,
        },
    }


class TestSlugifyTerm:
    def test_lowercases_and_replaces_spaces(self):
        assert slugify_term("Monthly Recurring Revenue") == "monthly_recurring_revenue"

    def test_strips_punctuation(self):
        assert slugify_term("Q4 (Actuals)!") == "q4_actuals"

    def test_collapses_repeated_separators(self):
        assert slugify_term("A -- B") == "a_b"

    def test_strips_leading_trailing_separators(self):
        assert slugify_term("  MRR  ") == "mrr"


class TestAssignGlossaryId:
    def test_builds_id_from_model_and_slug(self):
        used: set[str] = set()
        assert assign_glossary_id("MRR", "model-1", used) == "keboola/model-1/mrr"
        assert used == {"keboola/model-1/mrr"}

    def test_appends_numeric_suffix_on_collision(self):
        used = {"keboola/model-1/mrr"}
        assert assign_glossary_id("MRR", "model-1", used) == "keboola/model-1/mrr-2"
        assert "keboola/model-1/mrr-2" in used

    def test_appends_third_suffix_on_second_collision(self):
        used = {"keboola/model-1/mrr", "keboola/model-1/mrr-2"}
        assert assign_glossary_id("MRR", "model-1", used) == "keboola/model-1/mrr-3"


def _glossary_item(term, definition, see_also=None, model_uuid="model-1"):
    return {
        "type": "semantic-glossary",
        "id": "some-uuid",
        "attributes": {
            "term": term,
            "definition": definition,
            "seeAlso": see_also or [],
            "modelUUID": model_uuid,
        },
    }


class TestRelationshipLookupByDataset:
    def test_indexes_by_both_from_and_to(self):
        rel = _relationship_item("orders_to_customers", "in.c-a.orders", "in.c-a.customers", 'o."customer_id" = c."id"')
        lookup = relationship_lookup_by_dataset([rel])
        assert lookup["in.c-a.orders"] == [rel["attributes"]]
        assert lookup["in.c-a.customers"] == [rel["attributes"]]

    def test_empty_items_yields_empty_lookup(self):
        assert relationship_lookup_by_dataset([]) == {}


class TestResolveRelationship:
    def test_resolves_when_dataset_is_verified_to_side(self):
        rel_attrs = _relationship_item("o_to_c", "in.c-a.orders", "in.c-a.customers", 'o."customer_id" = c."id"')[
            "attributes"
        ]
        lookup = {"in.c-a.customers": [rel_attrs], "in.c-a.orders": [rel_attrs]}

        relationship, skip_reason = resolve_relationship("in.c-a.customers", lookup)

        assert skip_reason is None
        assert relationship == rel_attrs

    def test_skips_when_dataset_is_unverified_from_side(self):
        rel_attrs = _relationship_item("o_to_c", "in.c-a.orders", "in.c-a.customers", 'o."customer_id" = c."id"')[
            "attributes"
        ]
        lookup = {"in.c-a.customers": [rel_attrs], "in.c-a.orders": [rel_attrs]}

        relationship, skip_reason = resolve_relationship("in.c-a.orders", lookup)

        assert relationship is None
        assert skip_reason == "unverified_relationship_direction"

    def test_skips_when_no_relationship_touches_dataset(self):
        relationship, skip_reason = resolve_relationship("in.c-a.unrelated", {})
        assert relationship is None
        assert skip_reason == "ambiguous_relationship"

    def test_skips_when_multiple_relationships_touch_dataset(self):
        rel1 = _relationship_item("r1", "in.c-a.orders", "in.c-a.customers", 'o."x" = c."y"')["attributes"]
        rel2 = _relationship_item("r2", "in.c-a.payments", "in.c-a.customers", 'p."x" = c."z"')["attributes"]
        lookup = {"in.c-a.customers": [rel1, rel2]}

        relationship, skip_reason = resolve_relationship("in.c-a.customers", lookup)

        assert relationship is None
        assert skip_reason == "ambiguous_relationship"

    def test_skips_unsupported_relationship_type(self):
        rel_attrs = _relationship_item(
            "o_to_c", "in.c-a.orders", "in.c-a.customers", 'o."x" = c."y"', rel_type="inner"
        )["attributes"]
        lookup = {"in.c-a.customers": [rel_attrs]}

        relationship, skip_reason = resolve_relationship("in.c-a.customers", lookup)

        assert relationship is None
        assert skip_reason == "unsupported_relationship_type"


class TestParseOnClause:
    def test_parses_standard_shape(self):
        assert parse_on_clause('o."customer_id" = c."id"') == ("o", "customer_id", "c", "id")

    def test_handles_extra_whitespace(self):
        assert parse_on_clause('o."customer_id"   =   c."id"') == ("o", "customer_id", "c", "id")

    def test_returns_none_for_unrecognized_shape(self):
        assert parse_on_clause("o.customer_id = c.id") is None
        assert parse_on_clause("some garbage") is None


class TestResolveJoinAliases:
    def test_resolves_when_only_one_pairing_matches_known_columns(self):
        # to_columns (the metric's own table) has "id"; from_columns (the
        # joined table) has "customer_id" — only alias1=o/from, alias2=c/to
        # is consistent.
        on = 'o."customer_id" = c."id"'
        from_columns = {"customer_id", "name", "email"}
        to_columns = {"id", "order_date", "amount"}

        result = resolve_join_aliases(on, from_columns, to_columns)

        assert result == ("c", "o")  # (to_alias, from_alias)

    def test_resolves_reversed_operand_order(self):
        on = 'c."id" = o."customer_id"'
        from_columns = {"customer_id", "name"}
        to_columns = {"id", "order_date"}

        result = resolve_join_aliases(on, from_columns, to_columns)

        assert result == ("c", "o")

    def test_returns_none_when_both_pairings_match(self):
        # Both tables happen to have both column names — genuinely ambiguous.
        on = 'o."x" = c."y"'
        from_columns = {"x", "y"}
        to_columns = {"x", "y"}

        assert resolve_join_aliases(on, from_columns, to_columns) is None

    def test_returns_none_when_neither_pairing_matches(self):
        on = 'o."missing_a" = c."missing_b"'
        from_columns = {"customer_id"}
        to_columns = {"id"}

        assert resolve_join_aliases(on, from_columns, to_columns) is None

    def test_returns_none_for_unparseable_on_clause(self):
        assert resolve_join_aliases("garbage", {"a"}, {"b"}) is None


class TestExtractForeignAliases:
    def test_extracts_single_alias(self):
        assert extract_foreign_aliases('SUM(o."amount")') == {"o"}

    def test_extracts_multiple_distinct_aliases(self):
        # Live-verified real case: a metric used two distinct local alias
        # spellings for what resolved to the SAME single relationship.
        expr = 'CASE WHEN p."status" = \'x\' THEN SUM(pay."value") ELSE 0 END'
        assert extract_foreign_aliases(expr) == {"p", "pay"}

    def test_ignores_t_alias(self):
        assert extract_foreign_aliases('SUM(t."amount")') == set()

    def test_ignores_dotted_string_literal(self):
        assert extract_foreign_aliases("COUNT(CASE WHEN \"status\" = 'in.progress' THEN 1 END)") == set()


class TestComposeJoinSql:
    def test_composes_left_join_with_rewritten_aliases(self):
        expr = 'ROUND(SUM(TRY_CAST(o."amount" AS DECIMAL(18,2))), 2)'
        sql = compose_join_sql(
            expr,
            "crm_activities",
            "crm_opportunities",
            'o."opportunity_id" = a."id"',
            "a",
            "o",
        )
        assert sql == (
            'SELECT ROUND(SUM(TRY_CAST(j."amount" AS DECIMAL(18,2))), 2) '
            'FROM "crm_activities" AS t '
            'LEFT JOIN "crm_opportunities" AS j '
            'ON j."opportunity_id" = t."id"'
        )

    def test_rewrites_multiple_distinct_aliases_to_canonical_j(self):
        expr = 'CASE WHEN p."status" = \'x\' THEN SUM(pay."value") ELSE 0 END'
        sql = compose_join_sql(
            expr,
            "kbc_projects",
            "kbc_payg_payments",
            'p."project_id" = k."id"',
            "k",
            "p",
        )
        assert 'p."status"' not in sql
        assert 'pay."value"' not in sql
        # 2 from the rewritten expression (both distinct aliases -> j.) +
        # 1 from the composed ON clause's own j. reference.
        assert sql.count('j."') == 3

    def test_does_not_corrupt_alias_qualified_text_inside_a_quoted_literal(self):
        """Devin Review, PR #944: a string literal containing "<alias>." text
        (e.g. an enum-like value) must survive untouched — only real
        alias-qualified column references get rewritten."""
        expr = "CASE WHEN o.\"status\" = 'o.pending' THEN 1 ELSE 0 END"
        sql = compose_join_sql(
            expr,
            "crm_activities",
            "crm_opportunities",
            'o."opportunity_id" = a."id"',
            "a",
            "o",
        )
        assert "'o.pending'" in sql
        assert 'j."status"' in sql

    def test_does_not_corrupt_alias_qualified_text_inside_a_quoted_identifier(self):
        """Devin Review, PR #944: a quoted identifier containing "<alias>."
        text must survive untouched — only real alias-qualified column
        references get rewritten."""
        expr = 'SUM(o."o.legacy_amount")'
        sql = compose_join_sql(
            expr,
            "crm_activities",
            "crm_opportunities",
            'o."opportunity_id" = a."id"',
            "a",
            "o",
        )
        assert '"o.legacy_amount"' in sql


class TestTryJoinComposition:
    def test_composes_join_when_fully_resolvable(self):
        table_lookup = {
            ("in.c-a", "activities"): "crm_activities",
            ("in.c-a", "opportunities"): "crm_opportunities",
        }
        relationship_lookup = {
            "in.c-a.activities": [
                {
                    "from": "in.c-a.opportunities",
                    "to": "in.c-a.activities",
                    "on": 'o."id" = a."opportunity_id"',
                    "type": "left",
                }
            ],
        }
        column_lookup = {
            "crm_activities": {"opportunity_id", "created_at"},
            "crm_opportunities": {"id", "amount"},
        }

        result, skip_reason = try_join_composition(
            'SUM(o."amount")',
            "in.c-a.activities",
            table_lookup,
            relationship_lookup,
            column_lookup,
        )

        assert skip_reason is None
        assert result["table_name"] == "crm_activities"
        assert result["tables"] == ["crm_activities", "crm_opportunities"]
        assert 'FROM "crm_activities" AS t' in result["sql"]
        assert 'LEFT JOIN "crm_opportunities" AS j' in result["sql"]

    def test_falls_back_when_relationship_unresolvable(self):
        result, skip_reason = try_join_composition(
            'SUM(o."amount")',
            "in.c-a.orphan",
            {},
            {},
            {},
        )
        assert result is None
        assert skip_reason == "ambiguous_relationship"

    def test_falls_back_when_joined_table_not_registered(self):
        table_lookup = {("in.c-a", "activities"): "crm_activities"}
        relationship_lookup = {
            "in.c-a.activities": [
                {"from": "in.c-a.unregistered", "to": "in.c-a.activities", "on": 'o."x" = a."y"', "type": "left"}
            ],
        }
        result, skip_reason = try_join_composition(
            'SUM(o."x")',
            "in.c-a.activities",
            table_lookup,
            relationship_lookup,
            {},
        )
        assert result is None
        assert skip_reason == "foreign_alias_reference"

    def test_falls_back_when_column_metadata_missing(self):
        table_lookup = {
            ("in.c-a", "activities"): "crm_activities",
            ("in.c-a", "opportunities"): "crm_opportunities",
        }
        relationship_lookup = {
            "in.c-a.activities": [
                {
                    "from": "in.c-a.opportunities",
                    "to": "in.c-a.activities",
                    "on": 'o."id" = a."opportunity_id"',
                    "type": "left",
                }
            ],
        }
        result, skip_reason = try_join_composition(
            'SUM(o."amount")',
            "in.c-a.activities",
            table_lookup,
            relationship_lookup,
            {},
        )
        assert result is None
        assert skip_reason == "foreign_alias_reference"


def _relationship_metric_item(name, sql, dataset, model_uuid="model-1"):
    return {
        "type": "semantic-metric",
        "id": f"id-{name}",
        "attributes": {"name": name, "sql": sql, "dataset": dataset, "modelUUID": model_uuid},
    }


class TestBuildMetricRowWithRelationships:
    def test_resolves_join_metric_when_relationship_available(self):
        table_lookup = {
            ("in.c-a", "activities"): "crm_activities",
            ("in.c-a", "opportunities"): "crm_opportunities",
        }
        relationship_lookup = {
            "in.c-a.activities": [
                {
                    "from": "in.c-a.opportunities",
                    "to": "in.c-a.activities",
                    "on": 'o."id" = a."opportunity_id"',
                    "type": "left",
                }
            ],
        }
        column_lookup = {
            "crm_activities": {"opportunity_id"},
            "crm_opportunities": {"id", "amount"},
        }
        metric = _relationship_metric_item("linked_amount", 'SUM(o."amount")', "in.c-a.activities")

        row, skip_reason = build_metric_row(
            metric,
            table_lookup,
            {},
            [],
            "model-1",
            relationship_lookup=relationship_lookup,
            column_lookup=column_lookup,
        )

        assert skip_reason is None
        assert row["table_name"] == "crm_activities"
        assert row["tables"] == ["crm_activities", "crm_opportunities"]

    def test_falls_through_to_foreign_alias_reference_without_lookups(self):
        table_lookup = {("in.c-a", "activities"): "crm_activities"}
        metric = _relationship_metric_item("linked_amount", 'SUM(o."amount")', "in.c-a.activities")

        row, skip_reason = build_metric_row(metric, table_lookup, {}, [], "model-1")

        assert row is None
        assert skip_reason == "foreign_alias_reference"

    def test_single_table_metric_unaffected_by_new_params(self):
        table_lookup = {("in.c-a", "orders"): "crm_orders"}
        metric = _relationship_metric_item("total", 'SUM("amount")', "in.c-a.orders")

        row, skip_reason = build_metric_row(
            metric,
            table_lookup,
            {},
            [],
            "model-1",
            relationship_lookup={},
            column_lookup={},
        )

        assert skip_reason is None
        assert row["sql"] == 'SELECT SUM("amount") FROM "crm_orders" AS t'
        assert "tables" not in row


class TestBuildGlossaryRow:
    def test_builds_row_for_simple_term(self):
        used: set[str] = set()
        item = _glossary_item("Monthly Recurring Revenue", "Revenue normalized monthly.")

        row, skip_reason = build_glossary_row(item, "model-1", used)

        assert skip_reason is None
        assert row["id"] == "keboola/model-1/monthly_recurring_revenue"
        assert row["term"] == "Monthly Recurring Revenue"
        assert row["definition"] == "Revenue normalized monthly."
        assert row["see_also"] == []
        assert row["model_uuid"] == "model-1"
        assert row["source"] == "keboola_semantic_layer"

    def test_carries_see_also_list(self):
        used: set[str] = set()
        item = _glossary_item("MRR", "def", see_also=["arr", "churn"])

        row, skip_reason = build_glossary_row(item, "model-1", used)

        assert skip_reason is None
        assert row["see_also"] == ["arr", "churn"]

    def test_skips_missing_term(self):
        used: set[str] = set()
        item = _glossary_item(None, "def")

        row, skip_reason = build_glossary_row(item, "model-1", used)

        assert row is None
        assert skip_reason == "missing_term"

    def test_skips_missing_definition(self):
        used: set[str] = set()
        item = _glossary_item("MRR", None)

        row, skip_reason = build_glossary_row(item, "model-1", used)

        assert row is None
        assert skip_reason == "missing_definition"

    def test_second_call_with_colliding_slug_gets_suffix(self):
        used: set[str] = set()
        item_a = _glossary_item("MRR", "first def")
        item_b = _glossary_item("MRR", "second def, different casing collides on slug")

        row_a, _ = build_glossary_row(item_a, "model-1", used)
        row_b, _ = build_glossary_row(item_b, "model-1", used)

        assert row_a["id"] == "keboola/model-1/mrr"
        assert row_b["id"] == "keboola/model-1/mrr-2"
