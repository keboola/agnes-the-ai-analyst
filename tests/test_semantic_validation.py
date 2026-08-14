"""Case-table tests for the storage-independent semantic-layer query validator
(``src/semantic_validation.py``).

Design: docs/superpowers/specs/2026-08-14-semantic-layer-ui-and-agent-parity-design.md
(section 3, "Query validator") + the companion contract spec (section
"Background: Apache Ossie") for the document shape.

The engine is a pure transformation over a plain Python ``document`` dict (the
future ``semantic_models.document_json``), so these tests need no database —
they build one representative fixture document by hand and assert on the
validator's vendor-shape output. See ``_fixture_document`` below.
"""

from __future__ import annotations

import json

from src.semantic_validation import (
    AGNES_VENDOR_NAME,
    check_dialects,
    detect_used_objects,
    evaluate_constraints,
    extract_constraints,
    validate_query,
)

# --------------------------------------------------------------------------- #
# Fixture -- pending the storage slice (contract spec) landing. Shaped like a
# single entry of an Ossie ``semantic_model`` list, i.e. what will become one
# ``semantic_models.document_json`` row: 2 datasets (one with fields[] +
# ai_context incl. anti_keywords), 2 metrics (one DUCKDB expression, one
# SNOWFLAKE-only), 1 relationship, 1 glossary term, and constraints riding
# `custom_extensions` under the Agnes vendor name.
# --------------------------------------------------------------------------- #


def _agnes_extension(constraints: list[dict]) -> dict:
    return {"vendor_name": AGNES_VENDOR_NAME, "data": json.dumps({"constraints": constraints})}


def _fixture_document() -> dict:
    return {
        "name": "retail_model",
        "description": "Retail orders and customers.",
        "datasets": [
            {
                "name": "orders",
                "source": "analytics.orders",
                "primary_key": ["order_id"],
                "ai_context": {
                    "keywords": ["orders", "purchases"],
                    "synonyms": ["sales orders"],
                    "anti_keywords": ["draft_orders", "cart_items"],
                    "hints": ["Always filter by order_date for performance."],
                    "warnings": ["Do not use for refunds; see the refunds dataset."],
                },
                "fields": [
                    {"name": "order_id", "datatype": "String", "description": "Primary key."},
                    {"name": "order_date", "datatype": "Date", "description": "Order date."},
                    {"name": "customer_id", "datatype": "String", "description": "FK to customers."},
                ],
            },
            {
                "name": "customers",
                "source": "analytics.customers",
                "primary_key": ["customer_id"],
                "fields": [
                    {"name": "customer_id", "datatype": "String"},
                    {"name": "email", "datatype": "String"},
                ],
            },
        ],
        "relationships": [
            {
                "name": "orders_to_customers",
                "from": "orders",
                "to": "customers",
                "from_columns": ["customer_id"],
                "to_columns": ["customer_id"],
            },
        ],
        "metrics": [
            {
                "name": "revenue",
                "dataset": "orders",
                "expression": {"dialects": [{"dialect": "DUCKDB", "expression": "SUM(orders.amount)"}]},
                "datatype": "Decimal",
            },
            {
                "name": "customer_lifetime_value",
                "dataset": "customers",
                "expression": {
                    "dialects": [
                        {
                            "dialect": "SNOWFLAKE",
                            "expression": "SUM(amount) OVER (PARTITION BY customer_id)",
                        }
                    ]
                },
                "datatype": "Decimal",
            },
        ],
        "glossary": [
            {"term": "MRR", "definition": "Monthly recurring revenue.", "seeAlso": ["revenue"]},
        ],
        "custom_extensions": [
            _agnes_extension(
                [
                    {
                        "name": "revenue_requires_date_filter",
                        "type": "required_filter",
                        "rule": "order_date",
                        "severity": "error",
                        "metrics": ["revenue"],
                    },
                    {
                        "name": "revenue_non_negative",
                        "type": "value_range",
                        "rule": "value >= 0",
                        "severity": "warning",
                        "metrics": ["revenue"],
                    },
                ]
            )
        ],
    }


# --------------------------------------------------------------------------- #
# detect_used_objects
# --------------------------------------------------------------------------- #


class TestDetectUsedObjects:
    def test_matches_dataset_metric_and_relationship_by_name(self):
        sql = "SELECT revenue FROM orders o JOIN customers c ON o.customer_id = c.customer_id"
        result = detect_used_objects(sql, _fixture_document())
        assert result["used_datasets"] == ["orders", "customers"]
        assert result["used_metrics"] == ["revenue"]
        assert result["matched_relationships"] == ["orders_to_customers"]

    def test_matches_dataset_referenced_by_qualified_source_path(self):
        result = detect_used_objects("SELECT * FROM analytics.orders", _fixture_document())
        assert result["used_datasets"] == ["orders"]

    def test_matches_dataset_via_column_name_evidence_without_dataset_name(self):
        result = detect_used_objects("SELECT order_date FROM some_view", _fixture_document())
        assert result["used_datasets"] == ["orders"]

    def test_relationship_not_matched_when_only_one_side_used(self):
        result = detect_used_objects("SELECT revenue FROM orders", _fixture_document())
        assert result["used_datasets"] == ["orders"]
        assert result["matched_relationships"] == []

    def test_word_boundary_avoids_substring_false_positive(self):
        result = detect_used_objects("SELECT * FROM historical_orders_v2", _fixture_document())
        assert result["used_datasets"] == []

    def test_matching_is_case_insensitive(self):
        result = detect_used_objects("select REVENUE from ORDERS", _fixture_document())
        assert result["used_datasets"] == ["orders"]
        assert result["used_metrics"] == ["revenue"]

    def test_empty_sql_matches_nothing(self):
        result = detect_used_objects("", _fixture_document())
        assert result == {"used_datasets": [], "used_metrics": [], "matched_relationships": []}

    def test_document_with_no_objects_returns_empty(self):
        result = detect_used_objects("SELECT 1", {})
        assert result == {"used_datasets": [], "used_metrics": [], "matched_relationships": []}


# --------------------------------------------------------------------------- #
# extract_constraints
# --------------------------------------------------------------------------- #


class TestExtractConstraints:
    def test_extracts_constraints_from_agnes_vendor_extension(self):
        constraints = extract_constraints(_fixture_document())
        assert len(constraints) == 2
        by_name = {c["name"]: c for c in constraints}
        assert by_name["revenue_requires_date_filter"]["severity"] == "error"
        assert by_name["revenue_requires_date_filter"]["metrics"] == ["revenue"]
        assert by_name["revenue_non_negative"]["severity"] == "warning"

    def test_ignores_extensions_under_other_vendor_names(self):
        document = {
            "custom_extensions": [
                {
                    "vendor_name": "some_other_vendor",
                    "data": json.dumps({"constraints": [{"name": "x", "metrics": ["revenue"]}]}),
                }
            ]
        }
        assert extract_constraints(document) == []

    def test_tolerates_missing_custom_extensions(self):
        assert extract_constraints({}) == []

    def test_tolerates_custom_extensions_not_a_list(self):
        assert extract_constraints({"custom_extensions": "not-a-list"}) == []

    def test_tolerates_malformed_json_in_data(self):
        document = {"custom_extensions": [{"vendor_name": AGNES_VENDOR_NAME, "data": "{not json"}]}
        assert extract_constraints(document) == []

    def test_tolerates_data_not_a_string(self):
        document = {"custom_extensions": [{"vendor_name": AGNES_VENDOR_NAME, "data": {"constraints": []}}]}
        assert extract_constraints(document) == []

    def test_tolerates_constraints_key_missing_or_wrong_type(self):
        document = {
            "custom_extensions": [{"vendor_name": AGNES_VENDOR_NAME, "data": json.dumps({"constraints": "nope"})}]
        }
        assert extract_constraints(document) == []

    def test_defaults_severity_when_absent_or_invalid(self):
        document = {
            "custom_extensions": [
                _agnes_extension(
                    [
                        {"name": "c1", "metrics": ["revenue"]},
                        {"name": "c2", "severity": "critical", "metrics": ["revenue"]},
                    ]
                )
            ]
        }
        constraints = extract_constraints(document)
        assert {c["severity"] for c in constraints} == {"warning"}

    def test_accepts_constraints_as_bare_list_payload(self):
        document = {
            "custom_extensions": [
                {
                    "vendor_name": AGNES_VENDOR_NAME,
                    "data": json.dumps([{"name": "c1", "metrics": ["revenue"], "severity": "error"}]),
                }
            ]
        }
        constraints = extract_constraints(document)
        assert len(constraints) == 1
        assert constraints[0]["severity"] == "error"

    def test_document_not_a_dict_returns_empty(self):
        assert extract_constraints(None) == []  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# evaluate_constraints
# --------------------------------------------------------------------------- #


class TestEvaluateConstraints:
    def test_violation_when_required_filter_absent(self):
        constraints = extract_constraints(_fixture_document())
        violations, _post_checks = evaluate_constraints(constraints, ["revenue"], "SELECT SUM(amount) FROM orders")
        by_name = {v["name"]: v for v in violations}
        assert "revenue_requires_date_filter" in by_name
        assert by_name["revenue_requires_date_filter"]["severity"] == "error"

    def test_no_violation_when_required_filter_present(self):
        constraints = extract_constraints(_fixture_document())
        violations, _post_checks = evaluate_constraints(
            constraints,
            ["revenue"],
            "SELECT SUM(amount) FROM orders WHERE order_date >= '2026-01-01'",
        )
        assert "revenue_requires_date_filter" not in {v["name"] for v in violations}

    def test_statically_uncheckable_rule_degrades_to_post_execution(self):
        constraints = extract_constraints(_fixture_document())
        _violations, post_checks = evaluate_constraints(
            constraints,
            ["revenue"],
            "SELECT SUM(amount) FROM orders WHERE order_date >= '2026-01-01'",
        )
        assert "revenue_non_negative" in {c["name"] for c in post_checks}

    def test_constraint_skipped_when_its_metric_is_not_used(self):
        constraints = extract_constraints(_fixture_document())
        violations, post_checks = evaluate_constraints(constraints, ["customer_lifetime_value"], "SELECT 1")
        assert violations == []
        assert post_checks == []

    def test_empty_constraints_list(self):
        assert evaluate_constraints([], ["revenue"], "SELECT 1") == ([], [])


# --------------------------------------------------------------------------- #
# check_dialects
# --------------------------------------------------------------------------- #


class TestCheckDialects:
    def test_locally_executable_true_for_duckdb_only_metric(self):
        result = check_dialects(_fixture_document(), ["revenue"], target_engine="duckdb")
        assert result["sql_dialects"] == ["DUCKDB"]
        assert result["locally_executable"] is True
        assert result["mixed_dialect_warning"] is None

    def test_locally_executable_false_for_snowflake_only_metric(self):
        result = check_dialects(_fixture_document(), ["customer_lifetime_value"], target_engine="duckdb")
        assert result["sql_dialects"] == ["SNOWFLAKE"]
        assert result["locally_executable"] is False

    def test_mixed_dialect_warning_when_multiple_engines_used(self):
        result = check_dialects(_fixture_document(), ["revenue", "customer_lifetime_value"], target_engine="duckdb")
        assert set(result["sql_dialects"]) == {"DUCKDB", "SNOWFLAKE"}
        assert result["mixed_dialect_warning"] is not None
        assert result["locally_executable"] is False

    def test_no_used_metrics_means_no_dialects_and_locally_executable(self):
        result = check_dialects(_fixture_document(), [], target_engine="duckdb")
        assert result == {"sql_dialects": [], "mixed_dialect_warning": None, "locally_executable": True}

    def test_no_model_document_defaults_safe(self):
        result = check_dialects({}, ["revenue"], target_engine="duckdb")
        assert result == {"sql_dialects": [], "mixed_dialect_warning": None, "locally_executable": True}


# --------------------------------------------------------------------------- #
# validate_query (vendor-shape composition)
# --------------------------------------------------------------------------- #


class TestValidateQuery:
    def test_constraint_violation_drives_valid_false(self):
        result = validate_query("SELECT SUM(amount) AS revenue FROM orders", [_fixture_document()])
        assert result["valid"] is False
        assert any(v["name"] == "revenue_requires_date_filter" for v in result["violations"])
        assert result["used_datasets"] == ["orders"]
        assert result["used_metrics"] == ["revenue"]

    def test_query_with_required_filter_present_is_valid_but_has_post_execution_check(self):
        sql = "SELECT SUM(amount) AS revenue FROM orders WHERE order_date >= '2026-01-01'"
        result = validate_query(sql, [_fixture_document()])
        assert result["valid"] is True
        assert result["violations"] == []
        assert any(c["name"] == "revenue_non_negative" for c in result["post_execution_checks"])

    def test_locally_executable_false_for_snowflake_only_metric_query(self):
        sql = "SELECT customer_lifetime_value FROM customers"
        result = validate_query(sql, [_fixture_document()], target_engine="duckdb")
        assert result["locally_executable"] is False
        assert result["sql_dialects"] == ["SNOWFLAKE"]
        # Advisory only -- a dialect trap never flips validity by itself.
        assert result["valid"] is True

    def test_dialect_mix_warning_surfaces_in_summary(self):
        sql = (
            "SELECT revenue, customer_lifetime_value FROM orders "
            "JOIN customers ON orders.customer_id = customers.customer_id"
        )
        result = validate_query(sql, [_fixture_document()])
        assert set(result["sql_dialects"]) == {"DUCKDB", "SNOWFLAKE"}
        assert "mixed" in result["summary"].lower()

    def test_empty_documents_list_is_valid_and_empty(self):
        result = validate_query("SELECT 1", [])
        assert result["valid"] is True
        assert result["used_datasets"] == []
        assert result["used_metrics"] == []
        assert result["matched_relationships"] == []
        assert result["violations"] == []
        assert result["post_execution_checks"] == []
        assert result["sql_dialects"] == []
        assert result["locally_executable"] is True
        assert isinstance(result["summary"], str) and result["summary"]

    def test_empty_sql_is_valid_and_detects_nothing(self):
        result = validate_query("", [_fixture_document()])
        assert result["valid"] is True
        assert result["used_datasets"] == []
        assert result["used_metrics"] == []

    def test_malformed_custom_extensions_document_does_not_raise(self):
        document = dict(_fixture_document())
        document["custom_extensions"] = "not-a-list"
        result = validate_query("SELECT SUM(amount) AS revenue FROM orders", [document])
        assert result["violations"] == []
        assert result["post_execution_checks"] == []
        assert result["valid"] is True

    def test_expected_objects_diff(self):
        sql = "SELECT revenue FROM orders"
        expected = [
            {"type": "dataset", "name": "orders"},
            {"type": "metric", "name": "customer_lifetime_value"},
        ]
        result = validate_query(sql, [_fixture_document()], expected=expected)
        assert result["matched_expected_objects"] == [{"type": "dataset", "name": "orders"}]
        assert result["missing_expected_objects"] == [{"type": "metric", "name": "customer_lifetime_value"}]
        assert {"type": "metric", "name": "revenue"} in result["unexpected_detected_objects"]

    def test_expected_omitted_means_no_expected_keys_in_result(self):
        result = validate_query("SELECT 1", [_fixture_document()])
        assert "matched_expected_objects" not in result
        assert "missing_expected_objects" not in result
        assert "unexpected_detected_objects" not in result
