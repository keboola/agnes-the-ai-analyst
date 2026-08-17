"""BigQuery `materialize_query` writes parquet via BqAccess + DuckDB COPY.

The function takes a `BqAccess` instance so the BQ extension session and
SECRET token live in one place across the codebase (cf. `v2_scan` / `v2_sample`
/ `v2_schema`). Tests inject a stub BqAccess whose `duckdb_session()` yields
an in-memory connection with a pre-attached `bq` catalog containing fixture
tables, exercising the COPY path end-to-end without any GCP traffic.

The atomic-write section is routed through `src.parquet_publish` (#1359) —
`atomic_publish_temp_path` + `atomic_publish_finalize`, the manual two-step
form (this function's retry-on-timeout / lock-guarded structure spans too
much control flow to nest inside `atomic_publish`'s single `with` block).
Modeled on tests/test_parquet_publish.py / tests/test_jira_atomic_parquet_writes.py:
the failure stub writes the footerless bytes a killed COPY leaves AT THE PATH
THE STATEMENT TARGETS, then raises — a no-op stub that raises without
touching the filesystem first would pass even against code that isn't atomic
at all, which is the trap those tests' own history calls out explicitly.
"""

import os
import re
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import duckdb
import pytest

from connectors.bigquery.access import BqAccess, BqProjects
from connectors.bigquery.extractor import materialize_query

FOOTERLESS = b"PAR1" + b"\x00" * 64


class _ExecuteProxy:
    """Wraps a DuckDB connection so ``execute`` can be intercepted — DuckDB's
    native connection object doesn't allow monkeypatching bound methods
    directly (``AttributeError: attribute 'execute' is read-only``)."""

    def __init__(self, conn, on_execute):
        self._conn = conn
        self._on_execute = on_execute

    def execute(self, sql, *a, **kw):
        return self._on_execute(self._conn, sql, *a, **kw)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _boom_on_copy(real_conn, sql, *a, **kw):
    """The SQL-level equivalent of a killed `pq.write_table`: write footerless
    bytes AT THE PATH THE COPY STATEMENT TARGETS, then raise. Modeled on
    tests/test_parquet_publish.py's `_boom` — a stub that raises without
    touching the filesystem first would pass even against a writer with no
    temp-staging at all, which is not a test of atomicity."""
    upper = sql.upper()
    if "COPY" in upper and "FORMAT PARQUET" in upper:
        m = re.search(r"TO '([^']+)'", sql)
        assert m, f"could not find COPY target in: {sql}"
        Path(m.group(1)).write_bytes(FOOTERLESS)
        raise RuntimeError("simulated mid-COPY failure")
    return real_conn.execute(sql, *a, **kw)


def _make_stub_bq(tables: dict[str, str] | None = None, *, fail_copy: bool = False) -> BqAccess:
    """Return a BqAccess wired to factories that yield an in-memory DuckDB
    with a pretend `bq` catalog containing test tables. `tables` maps
    DuckDB-three-part references like `'bq.test.orders'` to a SELECT
    expression to seed them with.

    A `bigquery_query(project, sql_text)` table macro is registered so the
    wrapping added by `_wrap_admin_sql_for_jobs_api` (Task 2 — routes COPY
    through the BQ jobs API for views) resolves against the in-memory tables
    without needing the real BQ extension.

    `fail_copy=True` wraps the session connection so the materialize COPY
    dies midway — see `_boom_on_copy`.
    """
    tables = tables or {}

    @contextmanager
    def _session(_projects):
        conn = duckdb.connect(":memory:")
        try:
            conn.execute("ATTACH ':memory:' AS bq")
            schemas = {ref.rsplit(".", 1)[0] for ref in tables}
            for s in schemas:
                conn.execute(f"CREATE SCHEMA IF NOT EXISTS {s}")
            for ref, body in tables.items():
                conn.execute(f"CREATE OR REPLACE TABLE {ref} AS {body}")
            # Stub bigquery_query() so materialize_query's wrapped COPY works
            # against the in-memory bq catalog without the real BQ extension.
            conn.execute(
                "CREATE OR REPLACE MACRO bigquery_query(project, sql_text) AS TABLE SELECT * FROM query(sql_text)"
            )
            yield _ExecuteProxy(conn, _boom_on_copy) if fail_copy else conn
        finally:
            conn.close()

    # client_factory returns a stub whose .query(sql, job_config=...) yields
    # a job whose .total_bytes_processed defaults to 0 (fail-open).
    def _client(_projects):
        client = MagicMock()
        job = MagicMock()
        job.total_bytes_processed = 0
        client.query.return_value = job
        return client

    return BqAccess(
        BqProjects(billing="test-billing", data="test-data"),
        client_factory=_client,
        duckdb_session_factory=_session,
    )


def test_materialize_writes_parquet_and_returns_stats(tmp_path):
    out = tmp_path / "extracts" / "bigquery"
    out.mkdir(parents=True)

    bq = _make_stub_bq(
        {"bq.test.orders": ("SELECT 'EU' AS region, 100 AS revenue UNION ALL SELECT 'US' AS region, 250 AS revenue")}
    )

    stats = materialize_query(
        table_id="orders_summary",
        sql="SELECT region, SUM(revenue) AS revenue FROM bq.test.orders GROUP BY 1",
        bq=bq,
        output_dir=str(out),
    )

    parquet_path = out / "data" / "orders_summary.parquet"
    assert parquet_path.exists()
    assert stats["rows"] == 2
    assert stats["size_bytes"] > 0
    assert stats["query_mode"] == "materialized"

    # Parquet readable end-to-end
    rows = (
        duckdb.connect()
        .execute(f"SELECT region, revenue FROM read_parquet('{parquet_path}') ORDER BY region")
        .fetchall()
    )
    assert rows == [("EU", 100), ("US", 250)]


def test_materialize_atomic_on_failure(tmp_path):
    """Bad SQL must not leave a half-written parquet behind."""
    out = tmp_path / "extracts" / "bigquery"
    out.mkdir(parents=True)
    parquet_path = out / "data" / "broken.parquet"

    bq = _make_stub_bq({"bq.test.orders": "SELECT 1 AS n"})

    with pytest.raises(Exception):
        materialize_query(
            table_id="broken",
            sql="SELECT * FROM bq.test.does_not_exist",
            bq=bq,
            output_dir=str(out),
        )
    assert not parquet_path.exists()
    assert list((out / "data").glob("*.tmp")) == []


def test_materialize_killed_copy_leaves_previous_publish_intact(tmp_path):
    """#1359: a write that dies MIDWAY, not one that never starts — the
    footerless-bytes-then-raise stub `_boom_on_copy` models a SIGKILL'd COPY,
    not a bind-time SQL error like the test above. A no-op failure stub would
    pass even against a writer with no temp-staging at all."""
    out = tmp_path / "extracts" / "bigquery"
    out.mkdir(parents=True)
    parquet_path = out / "data" / "t1.parquet"
    parquet_path.parent.mkdir(parents=True)
    parquet_path.write_bytes(b"previously published")

    bq = _make_stub_bq({"bq.test.orders": "SELECT 1 AS n"}, fail_copy=True)

    with pytest.raises(RuntimeError, match="simulated mid-COPY failure"):
        materialize_query(table_id="t1", sql="SELECT n FROM bq.test.orders", bq=bq, output_dir=str(out))

    assert parquet_path.read_bytes() == b"previously published"
    assert list((out / "data").glob("*.tmp")) == []


def test_materialize_published_mode_is_0644_under_restrictive_umask(tmp_path):
    """DuckDB's COPY creates the temp as `0666 & umask`; a 0077 umask (seen in
    container/systemd units) would publish 0600 without the explicit chmod
    incident #203 documents."""
    out = tmp_path / "extracts" / "bigquery"
    out.mkdir(parents=True)
    bq = _make_stub_bq({"bq.test.orders": "SELECT 1 AS n"})

    previous = os.umask(0o077)
    try:
        materialize_query(table_id="t1", sql="SELECT n FROM bq.test.orders", bq=bq, output_dir=str(out))
    finally:
        os.umask(previous)

    pq_path = out / "data" / "t1.parquet"
    assert oct(pq_path.stat().st_mode & 0o777) == oct(0o644)


def test_materialize_temp_is_per_process_and_never_matches_the_parquet_glob(tmp_path, monkeypatch):
    out = tmp_path / "extracts" / "bigquery"
    out.mkdir(parents=True)
    bq = _make_stub_bq({"bq.test.orders": "SELECT 1 AS n"})

    real_replace = os.replace
    captured = {}

    def trace_replace(src, dst):
        captured["src"] = str(src)
        real_replace(src, dst)

    monkeypatch.setattr("connectors.bigquery.extractor.os.replace", trace_replace)
    materialize_query(table_id="t1", sql="SELECT n FROM bq.test.orders", bq=bq, output_dir=str(out))

    data_dir = out / "data"
    assert captured["src"] != str(data_dir / "t1.parquet"), "wrote straight onto the live path"
    assert str(os.getpid()) in Path(captured["src"]).name
    assert [p.name for p in data_dir.glob("*.parquet")] == ["t1.parquet"]
    # `*.parquet.*` also matches the deliberate `t1.parquet.lock` advisory
    # file lock — a different mechanism entirely — so check for a leftover
    # temp specifically.
    assert not list(data_dir.glob("*.tmp"))


def test_materialize_rejects_unsafe_table_id(tmp_path):
    """table_id becomes the parquet filename — block path traversal up front."""
    out = tmp_path / "extracts" / "bigquery"
    out.mkdir(parents=True)
    bq = _make_stub_bq()

    with pytest.raises(ValueError, match="unsafe"):
        materialize_query(
            table_id="../etc/passwd",
            sql="SELECT 1",
            bq=bq,
            output_dir=str(out),
        )


def test_materialize_overwrites_existing_parquet(tmp_path):
    out = tmp_path / "extracts" / "bigquery"
    out.mkdir(parents=True)
    bq = _make_stub_bq({"bq.test.tiny": "SELECT 1 AS n"})

    materialize_query(
        table_id="t1",
        sql="SELECT 1 AS n",
        bq=bq,
        output_dir=str(out),
    )
    materialize_query(
        table_id="t1",
        sql="SELECT 2 AS n",
        bq=bq,
        output_dir=str(out),
    )
    rows = duckdb.connect().execute(f"SELECT n FROM read_parquet('{out}/data/t1.parquet')").fetchall()
    assert rows == [(2,)]


def test_materialize_persists_meta_and_inner_view_in_extract_db(tmp_path):
    """0.40.0 fix: after materialize_query writes the parquet, it must also
    register the table in extract.duckdb (`_meta` row + inner view) so the
    orchestrator's master-view rebuild picks it up uniformly with remote-mode
    rows. Without this, the parquet sits on disk but the master view never
    materializes — `agnes query` 400s with "not yet materialized".
    """
    out = tmp_path / "extracts" / "bigquery"
    out.mkdir(parents=True)

    # Pre-create extract.duckdb (as the extractor subprocess would have done
    # on this connector's first pass) with the canonical _meta table + a
    # remote-mode row. We must verify the materialize call adds its row
    # without wiping the existing remote rows.
    extract_db = out / "extract.duckdb"
    with duckdb.connect(str(extract_db)) as ext:
        ext.execute("""CREATE TABLE _meta (
            table_name VARCHAR NOT NULL,
            description VARCHAR,
            rows BIGINT,
            size_bytes BIGINT,
            extracted_at TIMESTAMP,
            query_mode VARCHAR DEFAULT 'remote'
        )""")
        ext.execute("INSERT INTO _meta VALUES ('s1_session_landings', '', 0, 0, CURRENT_TIMESTAMP, 'remote')")

    bq = _make_stub_bq(
        {"bq.test.orders": ("SELECT 'EU' AS region, 100 AS revenue UNION ALL SELECT 'US' AS region, 250 AS revenue")}
    )

    materialize_query(
        table_id="orders_summary",
        sql="SELECT region, SUM(revenue) AS revenue FROM bq.test.orders GROUP BY 1",
        bq=bq,
        output_dir=str(out),
    )

    # Parquet exists.
    parquet_path = out / "data" / "orders_summary.parquet"
    assert parquet_path.exists()

    # _meta has BOTH the legacy remote row AND the new materialized row.
    with duckdb.connect(str(extract_db), read_only=True) as ext:
        rows = ext.execute("SELECT table_name, query_mode, rows FROM _meta ORDER BY table_name").fetchall()
        assert ("orders_summary", "materialized", 2) in [(r[0], r[1], r[2]) for r in rows]
        assert ("s1_session_landings", "remote", 0) in [(r[0], r[1], r[2]) for r in rows]
        # Inner view backing the master view exists, points at the parquet.
        view_rows = ext.execute('SELECT * FROM "orders_summary" ORDER BY region').fetchall()
        assert view_rows == [("EU", 100), ("US", 250)]


def test_materialize_replaces_meta_row_on_re_run(tmp_path):
    """A second materialize for the same table_id must REPLACE the existing
    `_meta` row, not duplicate it. Otherwise the orchestrator scan sees two
    rows for the same name and creates the master view twice (or worse,
    against stale row stats)."""
    out = tmp_path / "extracts" / "bigquery"
    out.mkdir(parents=True)
    # Pre-create extract.duckdb (the extractor subprocess would do this on
    # the first sync pass; we shortcut so the test exercises the
    # delete-then-insert branch on re-run, not the "no extract.duckdb yet"
    # skip branch.
    extract_db = out / "extract.duckdb"
    with duckdb.connect(str(extract_db)) as ext:
        ext.execute("""CREATE TABLE _meta (
            table_name VARCHAR NOT NULL,
            description VARCHAR,
            rows BIGINT,
            size_bytes BIGINT,
            extracted_at TIMESTAMP,
            query_mode VARCHAR DEFAULT 'remote'
        )""")

    bq = _make_stub_bq(
        {
            "bq.test.t1": "SELECT 'EU' AS region, 100 AS revenue",
            "bq.test.t2": ("SELECT 'EU' AS region, 100 AS revenue UNION ALL SELECT 'US' AS region, 250 AS revenue"),
        }
    )

    # First pass — 1 row.
    materialize_query(
        table_id="orders_summary",
        sql="SELECT region, revenue FROM bq.test.t1",
        bq=bq,
        output_dir=str(out),
    )
    # Second pass — different SQL, 2 rows. Must overwrite, not duplicate.
    materialize_query(
        table_id="orders_summary",
        sql="SELECT region, revenue FROM bq.test.t2",
        bq=bq,
        output_dir=str(out),
    )

    extract_db = out / "extract.duckdb"
    with duckdb.connect(str(extract_db), read_only=True) as ext:
        rows = ext.execute("SELECT COUNT(*), MAX(rows) FROM _meta WHERE table_name = 'orders_summary'").fetchone()
        assert rows[0] == 1, "must be exactly one _meta row, not duplicated"
        assert rows[1] == 2, "row count reflects the latest run, not the first"


def test_materialize_skips_inner_view_when_extract_db_missing(tmp_path):
    """Fresh BQ-only deployment may not have run the extractor subprocess
    yet, so extract.duckdb doesn't exist. materialize_query must not crash
    on that path — it logs and continues, the next extractor pass +
    rebuild will pick up the parquet via the registered registry row."""
    out = tmp_path / "extracts" / "bigquery"
    out.mkdir(parents=True)
    # Deliberately do NOT create extract.duckdb.

    bq = _make_stub_bq({"bq.test.t": "SELECT 1 AS n"})

    # Should NOT raise — fail-soft.
    stats = materialize_query(
        table_id="solo_table",
        sql="SELECT n FROM bq.test.t",
        bq=bq,
        output_dir=str(out),
    )
    assert stats["rows"] == 1
    # Parquet is on disk, extract.duckdb still doesn't exist (no force-create).
    assert (out / "data" / "solo_table.parquet").exists()
    assert not (out / "extract.duckdb").exists()
