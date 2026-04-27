"""Adversarial test corpus for the WHERE clause validator (spec §3.7)."""

import pytest
from app.api.where_validator import (
    validate_where,
    WhereValidationError,
    REJECT_NESTED_SELECT,
    REJECT_MULTI_STATEMENT,
    REJECT_DDL_DML,
    REJECT_PARSE,
    REJECT_CROSS_TABLE,
)


# A schema-like dict the validator uses to verify column references.
SCHEMA = {
    "event_date": "DATE",
    "country_code": "STRING",
    "session_id": "STRING",
    "amount": "INT64",
}
TABLE_ID = "web_sessions_example"


class TestParse:
    def test_empty_string_rejected(self):
        with pytest.raises(WhereValidationError) as e:
            validate_where("", TABLE_ID, SCHEMA)
        assert e.value.kind == REJECT_PARSE

    def test_unparseable_rejected(self):
        with pytest.raises(WhereValidationError) as e:
            validate_where("SELECT * FROM", TABLE_ID, SCHEMA)
        assert e.value.kind == REJECT_PARSE


class TestStructural:
    def test_nested_select_rejected(self):
        with pytest.raises(WhereValidationError) as e:
            validate_where(
                "country_code IN (SELECT country FROM other_table)",
                TABLE_ID, SCHEMA,
            )
        assert e.value.kind == REJECT_NESTED_SELECT

    def test_multi_statement_rejected(self):
        with pytest.raises(WhereValidationError) as e:
            validate_where("amount = 1; DROP TABLE x", TABLE_ID, SCHEMA)
        assert e.value.kind == REJECT_MULTI_STATEMENT

    def test_drop_table_rejected(self):
        with pytest.raises(WhereValidationError) as e:
            validate_where("amount = (DROP TABLE x)", TABLE_ID, SCHEMA)
        assert e.value.kind in (REJECT_DDL_DML, REJECT_PARSE)

    def test_cross_table_reference_rejected(self):
        """Predicates may only reference the target table."""
        with pytest.raises(WhereValidationError) as e:
            validate_where(
                "other_table.id = 1",
                TABLE_ID, SCHEMA,
            )
        assert e.value.kind == REJECT_CROSS_TABLE


class TestFunctionAllowList:
    @pytest.mark.parametrize(
        "predicate",
        [
            # Comparison
            "amount = 1", "amount != 1", "amount IS NULL", "amount IS NOT NULL",
            "country_code IN ('CZ', 'SK')", "amount BETWEEN 1 AND 100",
            "country_code LIKE 'C%'", "country_code NOT LIKE 'X%'",
            # Boolean
            "amount = 1 AND country_code = 'CZ'",
            "amount = 1 OR amount = 2",
            "NOT (amount = 1)",
            # Date/Time
            "event_date > DATE '2026-01-01'",
            "event_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)",
            "EXTRACT(YEAR FROM event_date) = 2026",
            # String
            "STARTS_WITH(country_code, 'C')",
            "REGEXP_CONTAINS(country_code, r'C[ZS]')",
            "LENGTH(country_code) = 2",
            # Math
            "amount > ABS(-5)",
            "amount BETWEEN GREATEST(0, 10) AND LEAST(100, 200)",
            # Cast
            "CAST(country_code AS STRING) = 'CZ'",
            # Conditional
            "IFNULL(country_code, 'XX') = 'CZ'",
            "COALESCE(amount, 0) > 0",
        ],
    )
    def test_allowed_predicate(self, predicate):
        # Add a fresh import here so this test class can be moved/copied easily
        from app.api.where_validator import validate_where
        validate_where(predicate, TABLE_ID, SCHEMA)  # must not raise

    @pytest.mark.parametrize(
        "predicate,expected_func",
        [
            ("amount = EXTERNAL_QUERY('connection', 'SELECT 1')", "EXTERNAL_QUERY"),
            ("country_code = SESSION_USER()", "SESSION_USER"),
            ("amount = OBSCURE_BUILTIN(country_code)", "OBSCURE_BUILTIN"),
        ],
    )
    def test_disallowed_function(self, predicate, expected_func):
        from app.api.where_validator import validate_where, REJECT_UNKNOWN_FUNCTION, WhereValidationError
        with pytest.raises(WhereValidationError) as e:
            validate_where(predicate, TABLE_ID, SCHEMA)
        assert e.value.kind == REJECT_UNKNOWN_FUNCTION
        assert expected_func.upper() in str(e.value).upper() or (
            e.value.detail and expected_func.upper() in str(e.value.detail).upper()
        )
