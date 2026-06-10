"""QueryResponse.bytes_scanned round-trip (#393).

The remote-query path surfaces the BigQuery dry-run scan estimate so REST,
CLI, and MCP consumers can show "how much did this scan". Local queries
involve no BQ tables, so the field stays ``None``.
"""

from app.api.query import QueryResponse


def test_query_response_round_trips_non_null_bytes_scanned():
    """Remote query: bytes_scanned carries the dry-run estimate through
    model construction and JSON serialization."""
    resp = QueryResponse(
        columns=["country", "n"],
        rows=[["CZ", 42]],
        row_count=1,
        truncated=False,
        bytes_scanned=4_500_000_000,
    )
    assert resp.bytes_scanned == 4_500_000_000
    dumped = resp.model_dump()
    assert dumped["bytes_scanned"] == 4_500_000_000
    # Field is part of the wire contract REST/MCP consumers see.
    assert "bytes_scanned" in QueryResponse.model_json_schema()["properties"]


def test_query_response_bytes_scanned_defaults_none_for_local():
    """Local query: omitting bytes_scanned defaults to None (no BQ tables)."""
    resp = QueryResponse(
        columns=["n"],
        rows=[[1]],
        row_count=1,
        truncated=False,
    )
    assert resp.bytes_scanned is None
    assert resp.model_dump()["bytes_scanned"] is None
