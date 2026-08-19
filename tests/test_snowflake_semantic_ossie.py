"""Snowflake semantic views -> Ossie adapter.

The composer is a pure function over `DESCRIBE SEMANTIC VIEW` rows, so every
mapping assertion here runs without a Snowflake account. The fetch half is
covered by SQL-shape tests (identifier quoting, scoping, escaping) plus a
fake DuckDB connection — the one thing not covered is the live wire shape of
`DESCRIBE SEMANTIC VIEW`, which is pinned to the documented column contract.
"""

from __future__ import annotations

import json

import pytest

from connectors.snowflake.semantic_ossie import (
    SnowflakeSemanticAdapter,
    compose_document,
    build_describe_sql,
    build_show_sql,
    rows_to_dicts,
)
from src.semantic.dialect import resolve_expression
from src.semantic.document_validation import validate_document

VIEW = {"database_name": "ANALYTICS", "schema_name": "PUBLIC", "name": "SALES", "comment": "Sales model"}


def _row(kind, name, parent, prop, value):
    return {
        "object_kind": kind,
        "object_name": name,
        "parent_entity": parent,
        "property": prop,
        "property_value": value,
    }


def _rows():
    return [
        _row(None, None, None, "COMMENT", "Sales model"),
        _row("TABLE", "ORDERS", None, "BASE_TABLE_DATABASE_NAME", "ANALYTICS"),
        _row("TABLE", "ORDERS", None, "BASE_TABLE_SCHEMA_NAME", "RAW"),
        _row("TABLE", "ORDERS", None, "BASE_TABLE_NAME", "ORDERS_T"),
        _row("TABLE", "ORDERS", None, "PRIMARY_KEY", "ORDER_ID"),
        _row("TABLE", "ORDERS", None, "COMMENT", "One row per order"),
        _row("TABLE", "CUSTOMERS", None, "BASE_TABLE_DATABASE_NAME", "ANALYTICS"),
        _row("TABLE", "CUSTOMERS", None, "BASE_TABLE_SCHEMA_NAME", "RAW"),
        _row("TABLE", "CUSTOMERS", None, "BASE_TABLE_NAME", "CUSTOMERS_T"),
        _row("DIMENSION", "ORDER_DATE", "ORDERS", "EXPRESSION", "o.order_date"),
        _row("DIMENSION", "ORDER_DATE", "ORDERS", "DATA_TYPE", "DATE"),
        _row("DIMENSION", "ORDER_DATE", "ORDERS", "COMMENT", "Date the order was placed"),
        _row("FACT", "AMOUNT", "ORDERS", "EXPRESSION", "o.amount"),
        _row("FACT", "AMOUNT", "ORDERS", "DATA_TYPE", "NUMBER(10,2)"),
        _row("METRIC", "TOTAL_REVENUE", "ORDERS", "EXPRESSION", "SUM(orders.amount)"),
        _row("METRIC", "TOTAL_REVENUE", "ORDERS", "COMMENT", "Gross revenue"),
        _row("METRIC", "PRIVATE_COUNT", "ORDERS", "EXPRESSION", "COUNT(*)"),
        _row("METRIC", "PRIVATE_COUNT", "ORDERS", "ACCESS_MODIFIER", "PRIVATE"),
        _row("RELATIONSHIP", "ORDERS_TO_CUSTOMERS", None, "TABLE", "ORDERS"),
        _row("RELATIONSHIP", "ORDERS_TO_CUSTOMERS", None, "FOREIGN_KEY", "CUSTOMER_ID"),
        _row("RELATIONSHIP", "ORDERS_TO_CUSTOMERS", None, "REF_TABLE", "CUSTOMERS"),
        _row("RELATIONSHIP", "ORDERS_TO_CUSTOMERS", None, "REF_KEY", "ID"),
        _row("CUSTOM_INSTRUCTIONS", None, None, "AI_SQL_GENERATION", "Always filter to shipped orders"),
        # `EXTENSION` is NOT in the documented object_kind list but a live
        # account emits it (name "CA", Cortex Analyst). It is the only place
        # join_type and the declared time dimensions appear at all.
        _row(
            "EXTENSION",
            "CA",
            None,
            "VALUE",
            (
                '{"tables": [{"name": "ORDERS", "time_dimensions": [{"name": "ORDER_DATE"}]}], '
                '"relationships": [{"name": "ORDERS_TO_CUSTOMERS", "join_type": "inner"}]}'
            ),
        ),
    ]


def _model(text):
    result = validate_document(text)
    assert result.ok, result.errors
    return result.parsed["semantic_model"][0]


def test_composed_document_is_schema_valid():
    assert validate_document(compose_document(VIEW, _rows())).ok


def test_model_name_is_fully_qualified_so_same_named_views_do_not_collide():
    # The importer keys storage on the model name and collapses duplicates
    # (src/semantic/importer.py::import_documents), so two semantic views
    # named SALES in different schemas must not compose the same name.
    other = {**VIEW, "schema_name": "FINANCE"}
    assert _model(compose_document(VIEW, _rows()))["name"] == "ANALYTICS.PUBLIC.SALES"
    assert _model(compose_document(other, _rows()))["name"] == "ANALYTICS.FINANCE.SALES"


def test_logical_tables_become_datasets_sourced_at_the_base_table():
    datasets = {d["name"]: d for d in _model(compose_document(VIEW, _rows()))["datasets"]}
    assert set(datasets) == {"ORDERS", "CUSTOMERS"}
    assert datasets["ORDERS"]["source"] == "ANALYTICS.RAW.ORDERS_T"
    assert datasets["ORDERS"]["primary_key"] == ["ORDER_ID"]
    assert datasets["ORDERS"]["description"] == "One row per order"


def test_dimensions_and_facts_become_fields_on_their_parent_dataset():
    datasets = {d["name"]: d for d in _model(compose_document(VIEW, _rows()))["datasets"]}
    fields = {f["name"]: f for f in datasets["ORDERS"]["fields"]}
    assert set(fields) == {"ORDER_DATE", "AMOUNT"}
    assert fields["ORDER_DATE"]["datatype"] == "Date"
    assert fields["ORDER_DATE"]["description"] == "Date the order was placed"
    assert fields["AMOUNT"]["datatype"] == "Decimal"
    assert json.loads(fields["AMOUNT"]["custom_extensions"][0]["data"])["object_kind"] == "FACT"
    # CUSTOMERS has no dimensions/facts of its own — it must still be a dataset.
    assert "fields" not in datasets["CUSTOMERS"]


def test_metrics_carry_snowflake_dialect_and_are_refused_for_local_execution():
    metrics = {m["name"]: m for m in _model(compose_document(VIEW, _rows()))["metrics"]}
    assert metrics["TOTAL_REVENUE"]["description"] == "Gross revenue"
    dialects = metrics["TOTAL_REVENUE"]["expression"]["dialects"]
    assert dialects == [{"dialect": "SNOWFLAKE", "expression": "SUM(orders.amount)"}]
    # Honesty gate: a Snowflake-flavour expression must NOT be spliceable into
    # a local DuckDB query. Mislabelling it ANSI_SQL would make it look usable.
    sql, reason = resolve_expression(metrics["TOTAL_REVENUE"]["expression"])
    assert sql is None and "SNOWFLAKE" in reason


def test_private_metrics_are_labelled_not_silently_kept_as_public():
    metrics = {m["name"]: m for m in _model(compose_document(VIEW, _rows()))["metrics"]}
    payload = json.loads(metrics["PRIVATE_COUNT"]["custom_extensions"][0]["data"])
    assert payload["access_modifier"] == "PRIVATE"


def test_relationships_map_foreign_key_to_ref_key():
    rel = _model(compose_document(VIEW, _rows()))["relationships"][0]
    assert rel["name"] == "ORDERS_TO_CUSTOMERS"
    assert rel["from"] == "ORDERS"
    assert rel["to"] == "CUSTOMERS"
    assert rel["from_columns"] == ["CUSTOMER_ID"]
    assert rel["to_columns"] == ["ID"]


def test_multi_column_keys_are_split():
    rows = [r for r in _rows() if r["object_kind"] != "RELATIONSHIP"] + [
        _row("RELATIONSHIP", "R", None, "TABLE", "ORDERS"),
        _row("RELATIONSHIP", "R", None, "FOREIGN_KEY", "(CUSTOMER_ID, REGION)"),
        _row("RELATIONSHIP", "R", None, "REF_TABLE", "CUSTOMERS"),
        _row("RELATIONSHIP", "R", None, "REF_KEY", "ID, REGION"),
    ]
    rel = _model(compose_document(VIEW, rows))["relationships"][0]
    assert rel["from_columns"] == ["CUSTOMER_ID", "REGION"]
    assert rel["to_columns"] == ["ID", "REGION"]


def test_view_comment_and_custom_instructions_are_kept():
    model = _model(compose_document(VIEW, _rows()))
    assert model["description"] == "Sales model"
    payload = json.loads(model["custom_extensions"][0]["data"])
    assert payload["semantic_view"] == "ANALYTICS.PUBLIC.SALES"
    assert payload["custom_instructions"]["AI_SQL_GENERATION"] == "Always filter to shipped orders"


def test_view_without_logical_tables_is_skipped_not_emitted_invalid():
    # Ossie requires >= 1 dataset; emitting one anyway would guarantee a
    # validation failure the operator then has to diagnose.
    assert compose_document(VIEW, [_row(None, None, None, "COMMENT", "empty")]) is None


def test_rows_to_dicts_uses_column_names_not_positions():
    # DuckDB hands back the Snowflake column order; keying off names means a
    # future column insertion upstream cannot silently shift the mapping.
    described = [("property_value",), ("object_kind",), ("property",), ("object_name",), ("parent_entity",)]
    rows = [("v", "TABLE", "COMMENT", "ORDERS", None)]
    assert rows_to_dicts(rows, described) == [
        {
            "object_kind": "TABLE",
            "object_name": "ORDERS",
            "parent_entity": None,
            "property": "COMMENT",
            "property_value": "v",
        }
    ]


def test_show_sql_scopes_to_database_by_default():
    assert build_show_sql(database="ANALYTICS", schema=None, like=None) == 'SHOW SEMANTIC VIEWS IN DATABASE "ANALYTICS"'


def test_show_sql_scopes_to_schema_and_pattern_when_given():
    sql = build_show_sql(database="ANALYTICS", schema="PUBLIC", like="SALES%")
    assert sql == 'SHOW SEMANTIC VIEWS LIKE \'SALES%\' IN SCHEMA "ANALYTICS"."PUBLIC"'


def test_show_sql_refuses_injection_in_scope_identifiers():
    with pytest.raises(ValueError):
        build_show_sql(database='A"; DROP TABLE X; --', schema=None, like=None)


def test_show_sql_escapes_quotes_in_the_like_pattern():
    assert "'' OR 1=1" in build_show_sql(database="A", schema=None, like="' OR 1=1")


def test_describe_sql_quotes_every_identifier_part():
    assert build_describe_sql(VIEW) == 'DESCRIBE SEMANTIC VIEW "ANALYTICS"."PUBLIC"."SALES"'


def test_describe_sql_refuses_an_unsafe_view_name():
    with pytest.raises(ValueError):
        build_describe_sql({**VIEW, "name": 'S"; DROP TABLE X; --'})


def test_adapter_is_registered_under_snowflake_semantic():
    from src.semantic.adapters import get_adapter

    assert isinstance(get_adapter("snowflake_semantic"), SnowflakeSemanticAdapter)


def test_adapter_raises_when_snowflake_is_not_configured(monkeypatch):
    # An unreachable source must raise, never return [] — the importer treats
    # an empty list as "upstream dropped everything" and prunes.
    monkeypatch.setattr(
        "connectors.snowflake.settings.resolve_snowflake_settings",
        lambda: None,
    )
    with pytest.raises(RuntimeError, match="not configured"):
        SnowflakeSemanticAdapter().extract({})


def test_declared_time_dimensions_set_the_temporal_role_flag():
    # Snowflake's own declaration is authoritative and covers what datatype
    # cannot: a year-grain Integer is a time dimension too.
    datasets = {d["name"]: d for d in _model(compose_document(VIEW, _rows()))["datasets"]}
    fields = {f["name"]: f for f in datasets["ORDERS"]["fields"]}
    assert fields["ORDER_DATE"]["dimension"] == {"is_time": True}
    assert "dimension" not in fields["AMOUNT"]


def test_join_type_survives_because_nothing_else_in_describe_carries_it():
    rel = _model(compose_document(VIEW, _rows()))["relationships"][0]
    assert json.loads(rel["custom_extensions"][0]["data"])["join_type"] == "inner"


def test_the_extension_payload_is_carried_whole():
    model = _model(compose_document(VIEW, _rows()))
    payload = json.loads(model["custom_extensions"][0]["data"])
    assert payload["extensions"]["CA"]["relationships"][0]["join_type"] == "inner"


def test_a_malformed_extension_payload_does_not_sink_the_document():
    rows = [r for r in _rows() if r["object_kind"] != "EXTENSION"]
    rows.append(_row("EXTENSION", "CA", None, "VALUE", "{not json"))
    text = compose_document(VIEW, rows)
    assert validate_document(text).ok
    # ...and the fields it would have annotated are still composed.
    datasets = {d["name"]: d for d in _model(text)["datasets"]}
    assert "ORDER_DATE" in {f["name"] for f in datasets["ORDERS"]["fields"]}


def test_public_access_is_not_restated_on_every_single_field():
    # A live view emitted ACCESS_MODIFIER=PUBLIC on all 61 fields. Recording
    # the default on every one buries the PRIVATE ones that actually matter.
    datasets = {d["name"]: d for d in _model(compose_document(VIEW, _rows()))["datasets"]}
    fields = {f["name"]: f for f in datasets["ORDERS"]["fields"]}
    rows = _rows() + [_row("FACT", "AMOUNT", "ORDERS", "ACCESS_MODIFIER", "PUBLIC")]
    public = {f["name"]: f for f in _model(compose_document(VIEW, rows))["datasets"][0]["fields"]}
    assert "access_modifier" not in json.loads(public["AMOUNT"]["custom_extensions"][0]["data"])
    assert json.loads(fields["AMOUNT"]["custom_extensions"][0]["data"])["object_kind"] == "FACT"


class _FakeConn:
    """Minimal DuckDB stand-in that records whether it was closed."""

    def __init__(self):
        self.closed = False

    def execute(self, *_args, **_kwargs):
        return self

    def close(self):
        self.closed = True


def test_the_scratch_connection_is_closed_when_attach_fails(monkeypatch):
    # A disallowed host, a bad key or a driver problem raises inside _connect,
    # AFTER the scratch DuckDB is already open. Leaking it there also leaves
    # the TemporaryDirectory tearing the file out from under a live handle.
    fake = _FakeConn()
    monkeypatch.setattr(
        "connectors.snowflake.settings.resolve_snowflake_settings",
        lambda: {
            "account": "acct",
            "user": "u",
            "database": "DB",
            "warehouse": "WH",
            "role": "",
            "password": "p",
        },
    )
    monkeypatch.setattr("src.duckdb_conn._open_duckdb", lambda *a, **k: fake)
    monkeypatch.setattr("connectors.snowflake.attach.install_snowflake_adbc_driver", lambda *a, **k: None)
    monkeypatch.setattr(
        "connectors.snowflake.attach.attach_snowflake",
        lambda *a, **k: (_ for _ in ()).throw(ValueError("host not allowed")),
    )

    with pytest.raises(ValueError, match="host not allowed"):
        SnowflakeSemanticAdapter().extract({})
    assert fake.closed is True


def test_an_unrecognized_object_kind_is_reported_not_swallowed(caplog):
    # A live account already returned one object_kind the SQL reference does
    # not document. The next one must leave a trail instead of vanishing.
    rows = _rows() + [_row("FILTER", "ONLY_SHIPPED", "ORDERS", "EXPRESSION", "status = 'S'")]
    with caplog.at_level("WARNING"):
        assert validate_document(compose_document(VIEW, rows)).ok
    # The message must say what is actually wrong. Falling through to the
    # "row with no object_name" branch would report a lie about a named row.
    reported = [r.getMessage() for r in caplog.records if "FILTER" in r.getMessage()]
    assert reported, "an unrecognized object_kind vanished without a trace"
    assert any("unrecognized object_kind" in m for m in reported), reported
    assert not any("no object_name" in m for m in reported), reported


def test_conflicting_duplicate_property_rows_are_reported(caplog):
    rows = _rows() + [_row("DIMENSION", "ORDER_DATE", "ORDERS", "COMMENT", "a different comment")]
    with caplog.at_level("WARNING"):
        compose_document(VIEW, rows)
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "ORDER_DATE" in joined and "COMMENT" in joined


def test_identical_duplicate_property_rows_are_not_noise(caplog):
    rows = _rows() + [_row("DIMENSION", "ORDER_DATE", "ORDERS", "COMMENT", "Date the order was placed")]
    with caplog.at_level("WARNING"):
        compose_document(VIEW, rows)
    assert not [r for r in caplog.records if "COMMENT" in r.getMessage()]
