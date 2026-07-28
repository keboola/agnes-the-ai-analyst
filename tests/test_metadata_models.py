"""Sanity tests for the shared metadata dataclasses."""

from app.api._metadata_models import MetadataRequest, TableMetadata


def test_metadata_request_constructs():
    req = MetadataRequest(
        table_id="orders", bucket="dwh_base", source_table="orders_2024",
    )
    assert req.table_id == "orders"
    assert req.bucket == "dwh_base"
    assert req.source_table == "orders_2024"


def test_metadata_request_is_frozen():
    """Frozen so cache keys derived from a request are stable."""
    req = MetadataRequest(table_id="x", bucket="b", source_table="t")
    import dataclasses
    try:
        req.bucket = "other"
    except dataclasses.FrozenInstanceError:
        return
    raise AssertionError("MetadataRequest should be frozen")


def test_table_metadata_all_fields_optional():
    tm = TableMetadata()
    assert tm.rows is None
    assert tm.size_bytes is None
    assert tm.partition_by is None
    assert tm.clustered_by is None


def test_table_metadata_partial_population():
    tm = TableMetadata(rows=100, size_bytes=2048)
    assert tm.rows == 100
    assert tm.size_bytes == 2048
    assert tm.partition_by is None
    assert tm.clustered_by is None
