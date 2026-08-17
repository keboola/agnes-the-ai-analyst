"""`src/ingest/tabular.py::ingest_tabular` — Group A publish site (#1359).

The DuckDB ``COPY (...) TO '<parquet_path>' (FORMAT PARQUET)`` step wrote
straight onto the parquet path that gets registered in `table_registry` and
served by the orchestrator's master views — the same class of exposure as the
Keboola full_refresh COPY, just for uploaded-file ingestion instead of a
scheduled sync.

Modeled on `tests/test_parquet_publish.py` / `tests/test_jira_atomic_parquet_writes.py`:
the failure stub writes the footerless bytes a killed COPY leaves AT THE PATH
THE STATEMENT TARGETS, then raises — a stub that raises without touching the
filesystem would pass against the unfixed (direct-write) code too.

`table_registry_repo().register(...)` and the post-write orchestrator rebuild
are stubbed out / left to fail-soft (the source already wraps the rebuild in a
``try/except``) so these tests stay focused on the parquet publish step and
don't need a real system.duckdb.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import duckdb
import pytest

from src.ingest import tabular

FOOTERLESS = b"PAR1" + b"\x00" * 64


class _FakeRegistry:
    def register(self, **kwargs):
        return None


@pytest.fixture(autouse=True)
def _stub_registry(monkeypatch):
    monkeypatch.setattr(tabular, "table_registry_repo", lambda: _FakeRegistry())


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    yield tmp_path


def _write_source_csv(tmp_path) -> str:
    src = tmp_path / "upload" / "sales.csv"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("id,amount\n1,100\n2,200\n")
    return str(src)


def _extracts_data_dir(data_dir, corpus_id: str):
    return data_dir / "extracts" / f"collection_{corpus_id}" / "data"


class _ExecuteProxy:
    """Wraps a DuckDB connection so `execute` can be intercepted — DuckDB's
    native connection object doesn't allow monkeypatching bound methods
    directly."""

    def __init__(self, conn, on_execute):
        self._conn = conn
        self._on_execute = on_execute

    def execute(self, sql, *a, **kw):
        return self._on_execute(self._conn, sql, *a, **kw)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _boom_on_copy(conn):
    def _on_execute(real_conn, sql, *a, **kw):
        upper = sql.upper()
        if "COPY" in upper and "FORMAT PARQUET" in upper:
            m = re.search(r"TO '([^']+)'", sql)
            assert m, f"could not find COPY target in: {sql}"
            Path(m.group(1)).write_bytes(FOOTERLESS)
            raise RuntimeError("simulated mid-COPY failure")
        return real_conn.execute(sql, *a, **kw)

    return _ExecuteProxy(conn, _on_execute)


def _tracing_on_copy(conn, seen: list[str]):
    def _on_execute(real_conn, sql, *a, **kw):
        if "COPY" in sql.upper():
            m = re.search(r"TO '([^']+)'", sql)
            if m:
                seen.append(m.group(1))
        return real_conn.execute(sql, *a, **kw)

    return _ExecuteProxy(conn, _on_execute)


def test_ingest_tabular_publishes_a_queryable_table(tmp_path):
    storage_path = _write_source_csv(tmp_path)

    table_id = tabular.ingest_tabular("corpus1", "cf_abc123", storage_path, "csv", filename="sales.csv")

    data_dir = _extracts_data_dir(tmp_path, "corpus1")
    pq_path = data_dir / f"{table_id}.parquet"
    assert pq_path.exists()
    rows = duckdb.connect().execute(f"SELECT COUNT(*) FROM read_parquet('{pq_path}')").fetchone()[0]
    assert rows == 2


def test_ingest_tabular_killed_copy_leaves_no_half_written_file(tmp_path, monkeypatch):
    storage_path = _write_source_csv(tmp_path)

    real_open_duckdb = tabular._open_duckdb

    def boom_open(*a, **kw):
        return _boom_on_copy(real_open_duckdb(*a, **kw))

    monkeypatch.setattr(tabular, "_open_duckdb", boom_open)

    with pytest.raises(RuntimeError, match="simulated mid-COPY failure"):
        tabular.ingest_tabular("corpus1", "cf_abc123", storage_path, "csv", filename="sales.csv")

    data_dir = _extracts_data_dir(tmp_path, "corpus1")
    assert list(data_dir.glob("*.parquet")) == []
    assert list(data_dir.glob("*.tmp")) == []


def test_ingest_tabular_published_mode_is_0644_under_restrictive_umask(tmp_path):
    storage_path = _write_source_csv(tmp_path)

    previous = os.umask(0o077)
    try:
        table_id = tabular.ingest_tabular("corpus1", "cf_abc123", storage_path, "csv", filename="sales.csv")
    finally:
        os.umask(previous)

    data_dir = _extracts_data_dir(tmp_path, "corpus1")
    pq_path = data_dir / f"{table_id}.parquet"
    assert oct(pq_path.stat().st_mode & 0o777) == oct(0o644)


def test_ingest_tabular_temp_is_per_process_and_never_matches_the_parquet_glob(tmp_path, monkeypatch):
    storage_path = _write_source_csv(tmp_path)

    seen: list[str] = []
    real_open_duckdb = tabular._open_duckdb

    def tracing_open(*a, **kw):
        return _tracing_on_copy(real_open_duckdb(*a, **kw), seen)

    monkeypatch.setattr(tabular, "_open_duckdb", tracing_open)
    table_id = tabular.ingest_tabular("corpus1", "cf_abc123", storage_path, "csv", filename="sales.csv")

    assert seen, "COPY was never executed"
    data_dir = _extracts_data_dir(tmp_path, "corpus1")
    assert seen[0] != str(data_dir / f"{table_id}.parquet"), "wrote straight onto the live path"
    assert str(os.getpid()) in Path(seen[0]).name
    assert [p.name for p in data_dir.glob("*.parquet")] == [f"{table_id}.parquet"]
    assert not list(data_dir.glob("*.parquet.*"))
