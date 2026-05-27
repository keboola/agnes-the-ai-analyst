"""End-to-end test for the profiler worker subprocess.

Drives ``src._profiler_worker`` over the runner — writes a tiny parquet
to a temp dir, invokes the worker with JSON args, and asserts the
returned profile dict has the shape downstream callers expect from
``profile_table``.
"""

import json
import os
from pathlib import Path

import duckdb
import pytest

from src._subprocess_runner import run_subprocess_job


@pytest.fixture
def tiny_parquet(tmp_path: Path) -> Path:
    """Materialize a 3-row, 2-column parquet ``profile_table`` can scan."""
    pq = tmp_path / "tiny.parquet"
    conn = duckdb.connect()
    try:
        safe = str(pq).replace("'", "''")
        conn.execute(
            f"COPY (SELECT * FROM (VALUES (1, 'a'), (2, 'b'), (3, 'c')) "
            f"AS t(id, label)) TO '{safe}' (FORMAT PARQUET)"
        )
    finally:
        conn.close()
    return pq


def test_worker_returns_profile_dict(tiny_parquet, tmp_path):
    # Worker runs in the same checkout as the parent, no PYTHONPATH override
    # required — but tests run inside the repo's venv which is on PATH.
    result = run_subprocess_job(
        "src._profiler_worker",
        {
            "table_name": "tiny",
            "table_id": "tiny",
            "parquet_path": str(tiny_parquet),
        },
        timeout_sec=60,
    )
    assert isinstance(result, dict)
    # ``profile_table`` returns a structure with at least these keys; if
    # the worker swallowed an exception or misroutes its output we'd see
    # an empty or wrong-shape dict here.
    assert "row_count" in result or "rows" in result or "columns" in result
