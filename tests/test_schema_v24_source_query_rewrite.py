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


def test_v24_logs_warning_when_project_not_configured(monkeypatch, caplog):
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
            with caplog.at_level("WARNING"):
                _ensure_schema(conn)
            row = conn.execute(
                "SELECT source_query FROM table_registry WHERE id='t1'"
            ).fetchone()
            assert row[0] == 'SELECT * FROM bq."ds"."tbl"'
            assert any(
                "v24" in r.message.lower() or "project" in r.message.lower()
                for r in caplog.records
            )
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
