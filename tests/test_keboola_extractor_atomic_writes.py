"""`connectors/keboola/extractor.py` Group A publish sites (#1359): the
extension COPY path (`_extract_via_extension`) and the legacy empty-placeholder
COPY (`_extract_via_legacy`) both used to write straight onto the SERVED
`<table>.parquet` path — the one every reader (`src/orchestrator.py`'s hasher,
the master views, `agnes pull`) globs and trusts is complete.

`_extract_via_extension` backs `sync_strategy='full_refresh'`, the primary
Keboola sync path, so a torn write here is the highest-severity site #1359
found: the exposure window runs from the COPY start until the WHOLE sync's
`sync_state` hash gets recomputed at the end of the run, not until this one
table's write finishes.

Modeled on `tests/test_parquet_publish.py` / `tests/test_jira_atomic_parquet_writes.py`:
the failure stub writes the footerless bytes a killed COPY leaves AT THE PATH
THE STATEMENT TARGETS, then raises — a stub that raises without touching the
filesystem would pass against the unfixed (direct-write) code too.
"""

import os
import re
from pathlib import Path

import duckdb
import pytest

from connectors.keboola.extractor import _extract_via_extension, _extract_via_legacy

FOOTERLESS = b"PAR1" + b"\x00" * 64


class _ExecuteProxy:
    """Wraps a DuckDB connection so ``execute`` can be intercepted.

    DuckDB's native connection object doesn't allow monkeypatching bound
    methods directly (``AttributeError: attribute 'execute' is read-only``),
    so callers that need to observe or fail specific statements go through
    this proxy instead. Every other attribute delegates straight to the
    wrapped connection.
    """

    def __init__(self, conn, on_execute):
        self._conn = conn
        self._on_execute = on_execute

    def execute(self, sql, *a, **kw):
        return self._on_execute(self._conn, sql, *a, **kw)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _boom_on_copy(conn: duckdb.DuckDBPyConnection) -> _ExecuteProxy:
    """Wrap *conn* so a ``COPY ... TO '<path>' (FORMAT PARQUET)`` statement
    first writes footerless bytes AT THE PATH THE STATEMENT TARGETS, then
    raises — the SQL-level equivalent of a `pq.write_table` killed midway."""

    def _on_execute(real_conn, sql, *a, **kw):
        upper = sql.upper()
        if "COPY" in upper and "FORMAT PARQUET" in upper:
            m = re.search(r"TO '([^']+)'", sql)
            assert m, f"could not find COPY target in: {sql}"
            Path(m.group(1)).write_bytes(FOOTERLESS)
            raise RuntimeError("simulated mid-COPY failure")
        return real_conn.execute(sql, *a, **kw)

    return _ExecuteProxy(conn, _on_execute)


def _kbc_conn_with_table(bucket: str, table: str, rows_sql: str) -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    conn.execute("ATTACH ':memory:' AS kbc")
    conn.execute(f'CREATE SCHEMA kbc."{bucket}"')
    conn.execute(f'CREATE TABLE kbc."{bucket}"."{table}" AS {rows_sql}')
    return conn


# --------------------------------------------------------------------------
# _extract_via_extension — the full_refresh COPY, previously written straight
# onto the served path with no temp at all.
# --------------------------------------------------------------------------


def test_extract_via_extension_publishes_the_full_table(tmp_path):
    conn = _kbc_conn_with_table("in.c-crm", "company", "SELECT 1 AS id, 'a' AS name")
    pq_path = str(tmp_path / "company.parquet")
    tc = {"bucket": "in.c-crm", "source_table": "company", "name": "company"}

    _extract_via_extension(conn, tc, pq_path)

    rows = duckdb.connect().execute(f"SELECT id, name FROM read_parquet('{pq_path}')").fetchall()
    assert rows == [(1, "a")]


def test_extract_via_extension_killed_copy_leaves_previous_publish_intact(tmp_path):
    conn = _kbc_conn_with_table("in.c-crm", "company", "SELECT 1 AS id")
    pq_path = tmp_path / "company.parquet"
    pq_path.write_bytes(b"previously published")
    tc = {"bucket": "in.c-crm", "source_table": "company", "name": "company"}

    conn = _boom_on_copy(conn)
    with pytest.raises(RuntimeError, match="simulated mid-COPY failure"):
        _extract_via_extension(conn, tc, str(pq_path))

    assert pq_path.read_bytes() == b"previously published"


def test_extract_via_extension_killed_copy_leaves_no_temp_behind(tmp_path):
    conn = _kbc_conn_with_table("in.c-crm", "company", "SELECT 1 AS id")
    pq_path = tmp_path / "company.parquet"
    tc = {"bucket": "in.c-crm", "source_table": "company", "name": "company"}

    conn = _boom_on_copy(conn)
    with pytest.raises(RuntimeError):
        _extract_via_extension(conn, tc, str(pq_path))

    assert not pq_path.exists()
    assert list(tmp_path.glob("*.tmp")) == []


def test_extract_via_extension_published_mode_is_0644_under_restrictive_umask(tmp_path):
    conn = _kbc_conn_with_table("in.c-crm", "company", "SELECT 1 AS id")
    pq_path = tmp_path / "company.parquet"
    tc = {"bucket": "in.c-crm", "source_table": "company", "name": "company"}

    previous = os.umask(0o077)
    try:
        _extract_via_extension(conn, tc, str(pq_path))
    finally:
        os.umask(previous)

    assert oct(pq_path.stat().st_mode & 0o777) == oct(0o644)


def test_extract_via_extension_writes_through_a_per_process_temp_invisible_to_the_glob(tmp_path):
    conn = _kbc_conn_with_table("in.c-crm", "company", "SELECT 1 AS id")
    pq_path = tmp_path / "company.parquet"
    tc = {"bucket": "in.c-crm", "source_table": "company", "name": "company"}

    seen: list[str] = []

    def _on_execute(real_conn, sql, *a, **kw):
        if "COPY" in sql.upper():
            m = re.search(r"TO '([^']+)'", sql)
            seen.append(m.group(1))
        return real_conn.execute(sql, *a, **kw)

    conn = _ExecuteProxy(conn, _on_execute)
    _extract_via_extension(conn, tc, str(pq_path))

    assert seen, "COPY was never executed"
    assert seen[0] != str(pq_path), "wrote straight onto the live path"
    assert str(os.getpid()) in Path(seen[0]).name
    assert [p.name for p in tmp_path.glob("*.parquet")] == ["company.parquet"]
    assert not list(tmp_path.glob("*.parquet.*"))


def test_extract_via_extension_two_concurrent_writers_do_not_clobber(tmp_path, monkeypatch):
    """The highest-severity site #1359 found (full_refresh is the primary
    Keboola sync path) gets the full concurrency scenario, not just the
    shared mechanism-level one in `tests/test_parquet_publish.py`."""
    pq_path = tmp_path / "company.parquet"
    tc = {"bucket": "in.c-crm", "source_table": "company", "name": "company"}

    from src.parquet_publish import atomic_publish_temp_path

    monkeypatch.setattr(os, "getpid", lambda: 11111)
    tmp_a = atomic_publish_temp_path(pq_path)
    tmp_a.parent.mkdir(parents=True, exist_ok=True)
    tmp_a.write_bytes(b"A-IN-FLIGHT")

    monkeypatch.setattr(os, "getpid", lambda: 22222)
    conn_b = _kbc_conn_with_table("in.c-crm", "company", "SELECT 2 AS id")
    _extract_via_extension(conn_b, tc, str(pq_path))

    assert tmp_a.read_bytes() == b"A-IN-FLIGHT", "writer B's publish touched writer A's temp"
    rows = duckdb.connect().execute(f"SELECT id FROM read_parquet('{pq_path}')").fetchall()
    assert rows == [(2,)]


# --------------------------------------------------------------------------
# _extract_via_legacy — empty-placeholder COPY, previously written straight
# onto the served path when the Storage API CSV export came back empty.
# --------------------------------------------------------------------------


class _FakeMetadataClient:
    def get_pyarrow_schema(self, table_id):
        return None

    def get_pandas_dtypes(self, table_id):
        return {}

    def get_date_columns(self, table_id):
        return []


class _FakeStorageClient:
    """`export_table_to_csv` that always reports an empty export — the branch
    that reaches the empty-placeholder COPY."""

    def export_table_to_csv(self, table_id, csv_path, export_filter=None):
        Path(csv_path).write_text("")
        return {"rows": 0}


@pytest.fixture
def _stub_legacy_clients(monkeypatch):
    # `_extract_via_legacy` does `from connectors.keboola.client import
    # KeboolaClient` at call time — kbcstorage is an optional dependency (see
    # tests/test_keboola_extractor_typed.py), so importing that module to
    # patch it must itself be skipped, not errored, when it's absent.
    pytest.importorskip("kbcstorage")
    monkeypatch.setattr("connectors.keboola.client.KeboolaClient", lambda **kw: _FakeMetadataClient())
    monkeypatch.setattr("connectors.keboola.storage_api.KeboolaStorageClient", lambda **kw: _FakeStorageClient())


@pytest.mark.usefixtures("_stub_legacy_clients")
def test_extract_via_legacy_empty_export_publishes_a_zero_row_parquet(tmp_path):
    pq_path = tmp_path / "company.parquet"
    tc = {"bucket": "in.c-crm", "source_table": "company", "name": "company"}

    _extract_via_legacy(tc, str(pq_path), "https://kbc.example", "tok")

    assert pq_path.exists()
    rows = duckdb.connect().execute(f"SELECT COUNT(*) FROM read_parquet('{pq_path}')").fetchone()[0]
    assert rows == 0


@pytest.mark.usefixtures("_stub_legacy_clients")
def test_extract_via_legacy_empty_placeholder_killed_write_leaves_previous_publish_intact(tmp_path, monkeypatch):
    pq_path = tmp_path / "company.parquet"
    pq_path.write_bytes(b"previously published")
    tc = {"bucket": "in.c-crm", "source_table": "company", "name": "company"}

    real_connect = duckdb.connect

    def boom_connect(*a, **kw):
        return _boom_on_copy(real_connect(*a, **kw))

    monkeypatch.setattr("connectors.keboola.extractor.duckdb.connect", boom_connect)

    with pytest.raises(RuntimeError, match="simulated mid-COPY failure"):
        _extract_via_legacy(tc, str(pq_path), "https://kbc.example", "tok")

    assert pq_path.read_bytes() == b"previously published"
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.usefixtures("_stub_legacy_clients")
def test_extract_via_legacy_empty_placeholder_published_mode_is_0644(tmp_path):
    pq_path = tmp_path / "company.parquet"
    tc = {"bucket": "in.c-crm", "source_table": "company", "name": "company"}

    previous = os.umask(0o077)
    try:
        _extract_via_legacy(tc, str(pq_path), "https://kbc.example", "tok")
    finally:
        os.umask(previous)

    assert oct(pq_path.stat().st_mode & 0o777) == oct(0o644)


@pytest.mark.usefixtures("_stub_legacy_clients")
def test_extract_via_legacy_empty_placeholder_temp_never_matches_the_parquet_glob(tmp_path):
    pq_path = tmp_path / "company.parquet"
    tc = {"bucket": "in.c-crm", "source_table": "company", "name": "company"}

    _extract_via_legacy(tc, str(pq_path), "https://kbc.example", "tok")

    assert [p.name for p in tmp_path.glob("*.parquet")] == ["company.parquet"]
    assert not list(tmp_path.glob("*.parquet.*"))
