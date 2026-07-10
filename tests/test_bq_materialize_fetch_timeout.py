"""Materialize fetch-phase timeout + single retry.

The BQ jobs API computes results in seconds; the failure mode observed in
production is the *client-side fetch* wedging indefinitely (dead HTTP
stream inside the DuckDB bigquery extension) — the extension's own
query timeout covers the BQ job, not the download. A wedged COPY holds
the per-table lock for hours and starves the daily schedule.

Contract: the COPY runs under a watchdog that interrupts the connection
after ``fetch_timeout_s`` and retries ONCE on a fresh session (a retry
was observed to complete in ~70 s where the first fetch hung for 2 h).
A second timeout propagates as ``MaterializeFetchTimeoutError``.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from unittest.mock import MagicMock

import duckdb
import pytest

import connectors.bigquery.extractor as mod
from connectors.bigquery.extractor import (
    MaterializeFetchTimeoutError,
    _execute_interruptible,
    materialize_query,
)


@pytest.fixture(autouse=True)
def reset_locks(monkeypatch):
    monkeypatch.setattr(mod, "_table_locks", {})
    yield


# ---------------------------------------------------------------- helper


def test_execute_interruptible_lets_fast_queries_through():
    con = duckdb.connect()
    _execute_interruptible(con, "SELECT 1", timeout_s=5.0)  # must not raise


def test_execute_interruptible_interrupts_overlong_query():
    con = duckdb.connect()
    con.execute("SET threads=1")
    start = time.monotonic()
    with pytest.raises(MaterializeFetchTimeoutError):
        _execute_interruptible(
            con,
            "SELECT sum(a.i * b.i) FROM range(100000) a(i), range(1000000) b(i)",
            timeout_s=0.3,
        )
    # The watchdog must fire near the deadline, not run the query to completion.
    assert time.monotonic() - start < 10


def test_execute_interruptible_zero_or_none_disables_watchdog():
    con = duckdb.connect()
    _execute_interruptible(con, "SELECT 1", timeout_s=0)
    _execute_interruptible(con, "SELECT 1", timeout_s=None)


# ---------------------------------------------------------------- retry


def _fake_bq(sessions_log: list):
    """BqAccess double whose duckdb_session yields SQL-pattern-matching
    fake connections (same shape as test_bq_materialize_concurrency)."""
    bq = MagicMock()
    bq.projects.billing = "prj-billing"
    bq.projects.data = "prj-data"

    class _Session:
        def __enter__(self):
            sessions_log.append(self)
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql):
            if sql.startswith("SELECT database_name"):
                r = MagicMock()
                r.fetchall.return_value = [("memory",)]
                return r
            if sql.startswith("ATTACH"):
                return MagicMock()
            if sql.startswith("COPY"):
                m = re.search(r"TO '([^']+)'", sql)
                assert m
                Path(m.group(1)).write_bytes(b"PARQUET_STUB" + b"\x00" * 64)
                return MagicMock()
            if sql.startswith("SELECT count"):
                r = MagicMock()
                r.fetchone.return_value = (7,)
                return r
            return MagicMock()

        def interrupt(self):
            pass

    bq.duckdb_session.side_effect = lambda: _Session()
    return bq


def test_materialize_retries_once_on_fetch_timeout(tmp_path, monkeypatch):
    sessions: list = []
    bq = _fake_bq(sessions)

    calls = {"n": 0}
    real_helper = mod._execute_interruptible

    def flaky_first_copy(conn, sql, timeout_s):
        if sql.startswith("COPY"):
            calls["n"] += 1
            if calls["n"] == 1:
                raise MaterializeFetchTimeoutError("stub-table", timeout_s=1.0)
        conn.execute(sql)

    monkeypatch.setattr(mod, "_execute_interruptible", flaky_first_copy)
    monkeypatch.setattr(mod, "_persist_materialized_inner_view", lambda **kw: None)

    stats = materialize_query(
        table_id="stub_table",
        sql="SELECT 1",
        bq=bq,
        output_dir=str(tmp_path),
        fetch_timeout_s=1.0,
    )

    assert calls["n"] == 2, "COPY must be retried exactly once after a fetch timeout"
    assert len(sessions) == 2, "the retry must run on a FRESH session, not the wedged one"
    assert stats["rows"] == 7
    assert (tmp_path / "data" / "stub_table.parquet").exists()
    assert real_helper is not mod._execute_interruptible  # sanity: patch was active


def test_materialize_raises_after_second_fetch_timeout(tmp_path, monkeypatch):
    sessions: list = []
    bq = _fake_bq(sessions)

    def always_timeout(conn, sql, timeout_s):
        if sql.startswith("COPY"):
            raise MaterializeFetchTimeoutError("stub-table", timeout_s=1.0)
        conn.execute(sql)

    monkeypatch.setattr(mod, "_execute_interruptible", always_timeout)

    with pytest.raises(MaterializeFetchTimeoutError):
        materialize_query(
            table_id="stub_table",
            sql="SELECT 1",
            bq=bq,
            output_dir=str(tmp_path),
            fetch_timeout_s=1.0,
        )

    assert len(sessions) == 2, "exactly one retry, then give up"
    assert not (tmp_path / "data" / "stub_table.parquet").exists()
    assert not (tmp_path / "data" / "stub_table.parquet.tmp").exists(), "tmp file must be cleaned up"
