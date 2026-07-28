import io
import pyarrow as pa
import pytest

from app.api.v2_arrow import arrow_table_to_ipc_bytes, parse_ipc_bytes


def test_round_trip_simple_table():
    src = pa.table({"a": [1, 2, 3], "b": ["x", "y", "z"]})
    body = arrow_table_to_ipc_bytes(src)
    assert isinstance(body, bytes) and len(body) > 0
    got = parse_ipc_bytes(body)
    assert got.equals(src)


def test_empty_table_round_trip():
    src = pa.table({"a": pa.array([], type=pa.int64())})
    body = arrow_table_to_ipc_bytes(src)
    got = parse_ipc_bytes(body)
    assert got.num_rows == 0
    assert got.schema.equals(src.schema)


def test_round_trip_record_batch_reader():
    src = pa.table({"a": [1, 2, 3], "b": ["x", "y", "z"]})
    reader = pa.RecordBatchReader.from_batches(src.schema, src.to_batches())
    body = arrow_table_to_ipc_bytes(reader)
    got = parse_ipc_bytes(body)
    assert got.equals(src)


class TestCappedSerialization:
    def test_under_cap_is_identical_to_uncapped(self):
        from app.api.v2_arrow import arrow_to_ipc_bytes_capped
        src = pa.table({"a": list(range(100))})
        assert arrow_to_ipc_bytes_capped(src, 10_000_000) == arrow_table_to_ipc_bytes(src)

    def test_reader_over_cap_is_truncated_and_parseable(self):
        from app.api.v2_arrow import arrow_to_ipc_bytes_capped
        src = pa.table({"a": list(range(10_000))})
        reader = pa.RecordBatchReader.from_batches(
            src.schema, src.to_batches(max_chunksize=512)
        )
        body = arrow_to_ipc_bytes_capped(reader, 4096)
        got = parse_ipc_bytes(body)
        assert 0 < got.num_rows < 2_000
        assert got.column("a").to_pylist() == list(range(got.num_rows))

    def test_empty_reader_yields_parseable_empty_stream(self):
        from app.api.v2_arrow import arrow_to_ipc_bytes_capped
        schema = pa.schema([("a", pa.int64())])
        reader = pa.RecordBatchReader.from_batches(schema, [])
        got = parse_ipc_bytes(arrow_to_ipc_bytes_capped(reader, 4096))
        assert got.num_rows == 0
        assert got.schema.equals(schema)

    def test_cap_below_schema_prelude_yields_empty_but_parseable(self):
        from app.api.v2_arrow import arrow_to_ipc_bytes_capped
        src = pa.table({"a": list(range(1_000))})
        got = parse_ipc_bytes(arrow_to_ipc_bytes_capped(src, 1))
        assert got.num_rows == 0
        assert got.schema.equals(src.schema)
