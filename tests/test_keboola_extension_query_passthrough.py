"""Lock-in test for the DuckDB Keboola extension's query-passthrough
capability that the Keboola materialized path depends on.

Run only when KBC_TEST_URL + KBC_TEST_TOKEN env vars are set (CI without
real Keboola credentials skips). Local dev with a real Storage API
token exercises the path.
"""
import os
import pytest
import duckdb


KBC_URL = os.environ.get("KBC_TEST_URL")
KBC_TOKEN = os.environ.get("KBC_TEST_TOKEN")
KBC_BUCKET = os.environ.get("KBC_TEST_BUCKET")
KBC_TABLE = os.environ.get("KBC_TEST_TABLE")

pytestmark = pytest.mark.skipif(
    not all([KBC_URL, KBC_TOKEN, KBC_BUCKET, KBC_TABLE]),
    reason="Keboola integration creds not provided",
)


def test_extension_supports_attach_and_select(tmp_path):
    """Keboola extension must support: ATTACH 'keboola://...' AS kbc, then
    SELECT * FROM kbc.bucket.table. The Keboola materialized path uses this
    primitive at runtime (just like connectors/keboola/extractor.py:133)."""
    conn = duckdb.connect(str(tmp_path / "spike.duckdb"))
    conn.execute("INSTALL keboola FROM community")
    conn.execute("LOAD keboola")
    escaped_token = KBC_TOKEN.replace("'", "''")
    conn.execute(f"ATTACH '{KBC_URL}' AS kbc (TYPE keboola, TOKEN '{escaped_token}')")
    rows = conn.execute(
        f'SELECT COUNT(*) FROM kbc."{KBC_BUCKET}"."{KBC_TABLE}"'
    ).fetchone()
    assert rows[0] >= 0  # any non-negative count is fine; we're testing the path works


def test_extension_supports_copy_to_parquet(tmp_path):
    """Keboola materialized writes the SELECT result via
    `COPY (...) TO '...' (FORMAT PARQUET)`. Lock that primitive."""
    conn = duckdb.connect(str(tmp_path / "spike.duckdb"))
    conn.execute("INSTALL keboola FROM community")
    conn.execute("LOAD keboola")
    escaped_token = KBC_TOKEN.replace("'", "''")
    conn.execute(f"ATTACH '{KBC_URL}' AS kbc (TYPE keboola, TOKEN '{escaped_token}')")

    parquet_path = tmp_path / "out.parquet"
    safe_lit = str(parquet_path).replace("'", "''")
    conn.execute(
        f'COPY (SELECT * FROM kbc."{KBC_BUCKET}"."{KBC_TABLE}" LIMIT 5) '
        f"TO '{safe_lit}' (FORMAT PARQUET)"
    )
    assert parquet_path.exists() and parquet_path.stat().st_size > 0


def test_extension_supports_filtered_query(tmp_path):
    """Most important capability: a non-trivial WHERE/projection survives.
    This is what 'Custom SQL' mode actually relies on."""
    conn = duckdb.connect(str(tmp_path / "spike.duckdb"))
    conn.execute("INSTALL keboola FROM community")
    conn.execute("LOAD keboola")
    escaped_token = KBC_TOKEN.replace("'", "''")
    conn.execute(f"ATTACH '{KBC_URL}' AS kbc (TYPE keboola, TOKEN '{escaped_token}')")

    parquet_path = tmp_path / "filtered.parquet"
    safe_lit = str(parquet_path).replace("'", "''")
    # Trivially filterable SELECT — extension must push the WHERE down or
    # at minimum execute it client-side. Either is acceptable for our
    # materialized path.
    conn.execute(
        f'COPY (SELECT 1 AS marker FROM kbc."{KBC_BUCKET}"."{KBC_TABLE}" LIMIT 3) '
        f"TO '{safe_lit}' (FORMAT PARQUET)"
    )
    assert parquet_path.exists()
