"""`app.utils.resolve_local_parquet` resolves a local table's parquet by the
extract DIRECTORY NAME, not by `source_type`.

Regression for the demo-instance bug: the bundled `demo` extract registers its
tables with `source_type='local'` but bakes its parquets under
`extracts/demo/data/`. The v2 endpoints used to key the path off `source_type`
(`extracts/local/data/<id>.parquet`), which does not exist, so `read_parquet`
crashed and `/api/v2/schema` (+ sample/scan) returned HTTP 500. The helper now
falls back to a source-name-agnostic rglob so any extract directory resolves.
"""

from pathlib import Path

import duckdb

from app.utils import resolve_local_parquet


def _make_parquet(dir_path: Path, table_id: str) -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    pq = dir_path / f"{table_id}.parquet"
    conn = duckdb.connect(":memory:")
    conn.execute(f"COPY (SELECT 1 AS a) TO '{pq}' (FORMAT PARQUET)")
    conn.close()
    return pq


def test_resolves_when_dir_matches_source_type(tmp_path: Path, monkeypatch):
    """Built-in connector layout (dir == source_type) — fast path hits."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    pq = _make_parquet(tmp_path / "extracts" / "keboola" / "data", "orders")
    assert resolve_local_parquet("orders", "keboola") == pq


def test_resolves_when_dir_differs_from_source_type(tmp_path: Path, monkeypatch):
    """The demo case: parquet under extracts/demo/ but source_type='local'.

    The source_type fast-path misses; the rglob fallback must still find it.
    This is the exact scenario that produced the 500 before the fix.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    pq = _make_parquet(tmp_path / "extracts" / "demo" / "data", "orders_demo")
    # No extracts/local/data/orders_demo.parquet exists — the old code looked there.
    assert not (tmp_path / "extracts" / "local" / "data" / "orders_demo.parquet").exists()
    assert resolve_local_parquet("orders_demo", "local") == pq


def test_resolves_without_source_type_hint(tmp_path: Path, monkeypatch):
    """Legacy rows may carry a NULL/empty source_type — rglob still resolves."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    pq = _make_parquet(tmp_path / "extracts" / "demo" / "data", "customers_demo")
    assert resolve_local_parquet("customers_demo", None) == pq
    assert resolve_local_parquet("customers_demo", "") == pq


def test_returns_none_when_missing(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "extracts").mkdir()
    assert resolve_local_parquet("nope", "local") is None


def test_returns_none_when_extracts_dir_absent(tmp_path: Path, monkeypatch):
    """No extracts tree at all (fresh instance) — None, never an exception."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    assert resolve_local_parquet("anything", "local") is None


def test_v2_schema_local_dir_mismatch_does_not_500(tmp_path: Path, monkeypatch):
    """End-to-end through build_schema_uncached: a source_type='local' row whose
    parquet lives under extracts/demo/ must yield a real schema, not raise."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    _make_parquet(tmp_path / "extracts" / "demo" / "data", "orders_demo")

    from app.api.v2_schema import build_schema_uncached
    row = {"id": "orders_demo", "source_type": "local", "query_mode": "local"}
    result = build_schema_uncached(
        conn=None, table_id="orders_demo", bq=object(), row=row,
    )
    assert result["sql_flavor"] == "duckdb"
    assert {c["name"] for c in result["columns"]} == {"a"}
