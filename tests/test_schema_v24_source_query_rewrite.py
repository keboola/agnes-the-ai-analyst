"""v24: rewrites table_registry.source_query for materialized BQ rows
from DuckDB-flavor (bq.\"ds\".\"tbl\") to BQ-native (`<project>.ds.tbl`).
The wrapping path (connectors.bigquery.extractor.materialize_query) only
accepts BQ-native; pre-v24 rows would fail at materialize time without
this conversion."""
from __future__ import annotations
import os
import tempfile
from pathlib import Path

import duckdb
import pytest

from src.db import _ensure_schema, get_schema_version, SCHEMA_VERSION


def _seed_v23(conn, project_id: str = "prj-data"):
    conn.execute(
        "CREATE TABLE schema_version (version INTEGER, applied_at TIMESTAMP DEFAULT current_timestamp)"
    )
    conn.execute("INSERT INTO schema_version (version) VALUES (23)")
    conn.execute(
        "CREATE TABLE table_registry ("
        "id VARCHAR PRIMARY KEY, name VARCHAR, source_type VARCHAR, "
        "query_mode VARCHAR, bucket VARCHAR, source_table VARCHAR, source_query VARCHAR)"
    )


def test_v24_rewrites_duckdb_flavor_to_bq_native(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("DATA_DIR", tmp)
        monkeypatch.setattr(
            "app.instance_config.get_value",
            lambda *args, **kw: "prj-data" if args == ("data_source", "bigquery", "project") else kw.get("default"),
        )
        Path(tmp, "state").mkdir(parents=True, exist_ok=True)
        db_path = Path(tmp, "state", "system.duckdb")
        conn = duckdb.connect(str(db_path))
        try:
            _seed_v23(conn)
            conn.execute(
                'INSERT INTO table_registry VALUES (?, ?, ?, ?, ?, ?, ?)',
                ["t1", "t1", "bigquery", "materialized", "ds", "tbl",
                 'SELECT * FROM bq."ds"."tbl"'],
            )
            conn.execute(
                'INSERT INTO table_registry VALUES (?, ?, ?, ?, ?, ?, ?)',
                ["t2", "t2", "bigquery", "materialized", "analytics", "orders",
                 'SELECT col1 FROM bq."analytics"."orders" WHERE col2 > 10'],
            )
            conn.execute(
                'INSERT INTO table_registry VALUES (?, ?, ?, ?, ?, ?, ?)',
                ["r1", "r1", "bigquery", "remote", "ds", "tbl", None],
            )

            _ensure_schema(conn)
            assert get_schema_version(conn) == SCHEMA_VERSION
            assert SCHEMA_VERSION >= 24

            rows = {r[0]: r[1] for r in conn.execute(
                "SELECT id, source_query FROM table_registry"
            ).fetchall()}
            assert rows["t1"] == "SELECT * FROM `prj-data.ds.tbl`"
            assert rows["t2"] == (
                "SELECT col1 FROM `prj-data.analytics.orders` WHERE col2 > 10"
            )
            assert rows["r1"] is None  # remote row untouched
        finally:
            conn.close()


def test_v24_idempotent_when_already_bq_native(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("DATA_DIR", tmp)
        monkeypatch.setattr(
            "app.instance_config.get_value",
            lambda *args, **kw: "prj-data" if args == ("data_source", "bigquery", "project") else kw.get("default"),
        )
        Path(tmp, "state").mkdir(parents=True, exist_ok=True)
        db_path = Path(tmp, "state", "system.duckdb")
        conn = duckdb.connect(str(db_path))
        try:
            _seed_v23(conn)
            conn.execute(
                'INSERT INTO table_registry VALUES (?, ?, ?, ?, ?, ?, ?)',
                ["t1", "t1", "bigquery", "materialized", "ds", "tbl",
                 "SELECT * FROM `prj-data.ds.tbl`"],
            )
            _ensure_schema(conn)
            row = conn.execute(
                "SELECT source_query FROM table_registry WHERE id='t1'"
            ).fetchone()
            assert row[0] == "SELECT * FROM `prj-data.ds.tbl`"
        finally:
            conn.close()


def test_v24_raises_when_project_not_configured_and_rows_need_migration(monkeypatch):
    """Regression for Devin Review on db.py:1757.

    Pre-fix: when v24 migration found rows to migrate but
    `data_source.bigquery.project` was empty, it logged a warning per
    row and returned normally. The schema_version then bumped to 24
    unconditionally → next start's `if current < 24:` gate skipped
    `_v23_to_v24_finalize` forever, leaving rows in DuckDB-flavor SQL
    that the new `_wrap_admin_sql_for_jobs_api` rejects as unparseable.
    The "set the project and restart to retry" log hint pointed at a
    code path that no longer ran.

    Post-fix: the migration raises a RuntimeError BEFORE the
    schema_version bump. The exception propagates out of `_ensure_schema`,
    blocking app startup with a clear actionable error. Operator
    configures the project, restarts, and the migration completes
    because schema_version is still 23.
    """
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("DATA_DIR", tmp)
        monkeypatch.setattr(
            "app.instance_config.get_value",
            lambda *args, **kw: kw.get("default", ""),  # no project configured
        )
        Path(tmp, "state").mkdir(parents=True, exist_ok=True)
        db_path = Path(tmp, "state", "system.duckdb")
        conn = duckdb.connect(str(db_path))
        try:
            _seed_v23(conn)
            conn.execute(
                'INSERT INTO table_registry VALUES (?, ?, ?, ?, ?, ?, ?)',
                ["t1", "t1", "bigquery", "materialized", "ds", "tbl",
                 'SELECT * FROM bq."ds"."tbl"'],
            )

            # Migration must REFUSE to bump schema_version when it can't
            # complete the row-rewrite. The error message must point the
            # operator at the right knob (`data_source.bigquery.project`).
            with pytest.raises(RuntimeError) as exc:
                _ensure_schema(conn)
            msg = str(exc.value)
            assert "data_source.bigquery.project" in msg
            assert "restart" in msg.lower()

            # Row stays in DuckDB-flavor (we couldn't rewrite it).
            row = conn.execute(
                "SELECT source_query FROM table_registry WHERE id='t1'"
            ).fetchone()
            assert row[0] == 'SELECT * FROM bq."ds"."tbl"'

            # Critical: schema_version stays at 23 so the migration retries
            # on the next startup once the operator configures the project.
            from src.db import get_schema_version
            assert get_schema_version(conn) == 23, (
                "schema_version must NOT bump to 24 when v24 migration is "
                "deferred — otherwise the documented retry path is dead"
            )
        finally:
            conn.close()


def test_v24_skips_clean_when_no_rows_match_even_without_project(monkeypatch):
    """Counterpart to the raise-on-deferred test: a deployment with NO
    materialized BQ rows (typical Keboola-only or remote-only install)
    must NOT block startup just because `data_source.bigquery.project`
    isn't configured. The `if not rows: return` early-out at the top of
    `_v23_to_v24_finalize` handles this — the raise only fires when
    there's actual work to defer."""
    import pytest as _pytest  # noqa: F401  (referenced if test extends later)

    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("DATA_DIR", tmp)
        monkeypatch.setattr(
            "app.instance_config.get_value",
            lambda *args, **kw: kw.get("default", ""),
        )
        Path(tmp, "state").mkdir(parents=True, exist_ok=True)
        db_path = Path(tmp, "state", "system.duckdb")
        conn = duckdb.connect(str(db_path))
        try:
            _seed_v23(conn)
            # No materialized BQ rows seeded.

            # Must NOT raise — there's nothing to migrate.
            _ensure_schema(conn)

            from src.db import get_schema_version, SCHEMA_VERSION
            assert get_schema_version(conn) == SCHEMA_VERSION
        finally:
            conn.close()


def test_v24_keboola_materialized_row_not_rewritten(monkeypatch):
    """Materialized rows with source_type != 'bigquery' must not be touched
    by v24. Keboola materialized has no notion of bq."ds"."tbl" syntax;
    the SELECT's source_type filter pins this contract.
    """
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("DATA_DIR", tmp)
        monkeypatch.setattr(
            "app.instance_config.get_value",
            lambda *args, **kw: "prj-data" if args == ("data_source", "bigquery", "project") else kw.get("default"),
        )
        Path(tmp, "state").mkdir(parents=True, exist_ok=True)
        db_path = Path(tmp, "state", "system.duckdb")
        conn = duckdb.connect(str(db_path))
        try:
            _seed_v23(conn)
            # Keboola row that happens to contain `bq."..."` in its SQL
            # (admin error or copy-paste from a BQ row). Migration must
            # leave it alone — this is not the v24 contract.
            conn.execute(
                'INSERT INTO table_registry VALUES (?, ?, ?, ?, ?, ?, ?)',
                ["kb1", "kb1", "keboola", "materialized", "ds", "tbl",
                 'SELECT * FROM bq."ds"."tbl"'],
            )
            _ensure_schema(conn)
            row = conn.execute(
                "SELECT source_query FROM table_registry WHERE id='kb1'"
            ).fetchone()
            assert row[0] == 'SELECT * FROM bq."ds"."tbl"'
        finally:
            conn.close()
