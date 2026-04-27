"""Arrow IPC serialization helpers for /api/v2/scan responses.

Server side serializes a pyarrow.Table to IPC stream bytes; client side
deserializes back. Content-Type is `application/vnd.apache.arrow.stream`.
"""

from __future__ import annotations
import io
import pyarrow as pa


CONTENT_TYPE = "application/vnd.apache.arrow.stream"


def arrow_table_to_ipc_bytes(table: pa.Table) -> bytes:
    """Serialize a pyarrow.Table to Arrow IPC stream bytes."""
    sink = io.BytesIO()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        for batch in table.to_batches():
            writer.write_batch(batch)
    return sink.getvalue()


def parse_ipc_bytes(data: bytes) -> pa.Table:
    """Deserialize Arrow IPC stream bytes to a pyarrow.Table."""
    reader = pa.ipc.open_stream(io.BytesIO(data))
    return reader.read_all()
