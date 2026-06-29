"""Regression: the Postgres backend's _decode_row must JSON-decode the
``platforms`` / ``gotchas`` TEXT columns AND normalize NULL / empty / parse
failures to ``[]`` for ALL four list-shaped docs fields, matching the DuckDB
backend's behaviour byte-for-byte.

The original bug (caught live on a Postgres deployment): ``platforms`` /
``gotchas`` are stored as TEXT holding a ``json.dumps()``'d value (the JSONB
columns ``sample_questions`` / ``pairs_well_with`` arrive pre-decoded from the
driver, but TEXT doesn't), so reads returned the raw JSON string and the
catalog table-page UI iterated it character-by-character.

The follow-up gap (Devin Review on #582, ANALYSIS_0001): the original fix only
JSON-decoded strings; it left ``None`` as-is and ignored
``sample_questions``/``pairs_well_with`` entirely, while the DuckDB backend
normalizes ``None``/empty-string/parse-failures to ``[]`` for all four keys.
Current consumers were safe via ``or []`` guards in the templates and routers,
but the latent parity gap would bite the first consumer that didn't add the
guard. The PG ``_decode_row`` now mirrors DuckDB exactly — these tests lock
that parity in.
"""
from __future__ import annotations

from src.repositories.table_registry_pg import TableRegistryPgRepository


def test_decode_row_parses_platforms_and_gotchas_text_columns():
    decoded = TableRegistryPgRepository._decode_row(
        {
            "id": "t",
            "platforms": '["web", "app"]',
            "gotchas": '[{"key": false, "body": "note"}]',
        }
    )
    assert decoded["platforms"] == ["web", "app"]
    assert decoded["gotchas"] == [{"key": False, "body": "note"}]


def test_decode_row_passthrough_native_lists():
    """JSONB columns arrive pre-decoded from the driver; pass through as-is."""
    decoded = TableRegistryPgRepository._decode_row(
        {
            "platforms": ["web"],
            "gotchas": [{"body": "x"}],
            "sample_questions": ["q1"],
            "pairs_well_with": ["table_a"],
        }
    )
    assert decoded["platforms"] == ["web"]
    assert decoded["gotchas"] == [{"body": "x"}]
    assert decoded["sample_questions"] == ["q1"]
    assert decoded["pairs_well_with"] == ["table_a"]


def test_decode_row_normalizes_none_to_empty_list_for_all_keys():
    """Parity with DuckDB: NULL → [] for every list-shaped docs field."""
    decoded = TableRegistryPgRepository._decode_row(
        {
            "platforms": None,
            "gotchas": None,
            "sample_questions": None,
            "pairs_well_with": None,
        }
    )
    assert decoded["platforms"] == []
    assert decoded["gotchas"] == []
    assert decoded["sample_questions"] == []
    assert decoded["pairs_well_with"] == []


def test_decode_row_normalizes_empty_string_to_empty_list():
    """Parity with DuckDB: legacy empty-string sentinel → []."""
    decoded = TableRegistryPgRepository._decode_row(
        {"platforms": "", "gotchas": ""}
    )
    assert decoded["platforms"] == []
    assert decoded["gotchas"] == []


def test_decode_row_normalizes_parse_failure_to_empty_list():
    """Parity with DuckDB: unparseable → [] (not ``"notjson"`` left raw)."""
    decoded = TableRegistryPgRepository._decode_row(
        {"platforms": "notjson", "gotchas": "{not valid json"}
    )
    assert decoded["platforms"] == []
    assert decoded["gotchas"] == []


def test_decode_row_normalizes_non_list_parsed_value_to_empty_list():
    """Parity with DuckDB: parsed-but-not-a-list (e.g. JSON object/scalar) → []."""
    decoded = TableRegistryPgRepository._decode_row(
        {"platforms": '{"not": "a list"}', "gotchas": '"just a string"'}
    )
    assert decoded["platforms"] == []
    assert decoded["gotchas"] == []
