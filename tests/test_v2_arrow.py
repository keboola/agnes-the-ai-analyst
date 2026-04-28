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
