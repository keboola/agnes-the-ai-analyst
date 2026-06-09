"""Regression: the Postgres backend's _decode_row must JSON-decode the
``platforms`` / ``gotchas`` TEXT columns. The JSONB-backed
``sample_questions`` / ``pairs_well_with`` arrive already-decoded from the
driver, but ``platforms`` / ``gotchas`` are TEXT holding a json.dumps()'d
value — without decoding, reads return the raw JSON string and the catalog
table-page UI iterates it character-by-character. Mirrors the DuckDB backend.
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
    decoded = TableRegistryPgRepository._decode_row(
        {"platforms": ["web"], "gotchas": [{"body": "x"}]}
    )
    assert decoded["platforms"] == ["web"]
    assert decoded["gotchas"] == [{"body": "x"}]


def test_decode_row_tolerates_none_and_unparseable():
    decoded = TableRegistryPgRepository._decode_row(
        {"platforms": None, "gotchas": "notjson"}
    )
    assert decoded["platforms"] is None
    assert decoded["gotchas"] == "notjson"
