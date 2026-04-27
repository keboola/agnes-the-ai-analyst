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
