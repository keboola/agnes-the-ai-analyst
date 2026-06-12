"""Arrow IPC serialization helpers for /api/v2/scan responses.

Server side serializes a pyarrow.Table to IPC stream bytes; client side
deserializes back. Content-Type is `application/vnd.apache.arrow.stream`.
"""

from __future__ import annotations
import io
import pyarrow as pa


CONTENT_TYPE = "application/vnd.apache.arrow.stream"


def arrow_table_to_ipc_bytes(source: pa.Table | pa.RecordBatchReader) -> bytes:
    """Serialize a pyarrow.Table or RecordBatchReader to Arrow IPC stream bytes."""
    sink = io.BytesIO()
    if isinstance(source, pa.RecordBatchReader):
        with pa.ipc.new_stream(sink, source.schema) as writer:
            for batch in source:
                writer.write_batch(batch)
    else:
        with pa.ipc.new_stream(sink, source.schema) as writer:
            for batch in source.to_batches():
                writer.write_batch(batch)
    return sink.getvalue()


def arrow_to_ipc_bytes_capped(
    source: pa.Table | pa.RecordBatchReader,
    max_bytes: int,
) -> bytes:
    """Serialize to Arrow IPC stream bytes, truncating near `max_bytes`.

    Streams batch-by-batch, so a RecordBatchReader (what duckdb>=1.5
    `.arrow()` returns) is consumed only up to the cap — an over-cap result
    is never fully materialized in memory. A batch that would cross the cap
    is sliced proportionally (avg-bytes-per-row heuristic, same approximation
    the old Table-only truncation used), so the result can exceed the cap
    only by per-batch IPC framing overhead.
    """
    batches = source if isinstance(source, pa.RecordBatchReader) else source.to_batches()
    sink = io.BytesIO()
    with pa.ipc.new_stream(sink, source.schema) as writer:
        for batch in batches:
            budget = max_bytes - sink.tell()
            if budget <= 0:
                break
            if batch.num_rows and batch.nbytes > budget:
                avg = max(1, batch.nbytes // batch.num_rows)
                keep = budget // avg
                if keep > 0:
                    writer.write_batch(batch.slice(0, keep))
                break
            writer.write_batch(batch)
    return sink.getvalue()


def parse_ipc_bytes(data: bytes) -> pa.Table:
    """Deserialize Arrow IPC stream bytes to a pyarrow.Table."""
    reader = pa.ipc.open_stream(io.BytesIO(data))
    return reader.read_all()
