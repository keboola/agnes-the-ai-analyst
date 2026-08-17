"""Tests for the test-harness ``duckdb.connect`` block-size wrapper.

``tests/conftest.py`` patches ``duckdb.connect`` so every DuckDB file created
inside the test process uses 16 KiB blocks instead of DuckDB's 256 KiB
default. A fresh ~217-table ``system.duckdb`` weighs ~7 MB at the default —
multiplied by ~5k per-test databases, one full local run retained ~47 GB of
basetemp. Small blocks halve the files and make the schema DDL ~3× faster.
These tests pin that contract: plain connects get small blocks, explicit
configs win, and existing default-block files still open.
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb
import pytest

from src.duckdb_conn import _open_duckdb

pytestmark = pytest.mark.skipif(
    os.environ.get("AGNES_TEST_DUCKDB_BLOCK_SIZE") == "0",
    reason="harness block-size wrapper explicitly disabled via AGNES_TEST_DUCKDB_BLOCK_SIZE=0",
)


def _file_block_size(path: Path) -> int:
    conn = duckdb.connect(str(path), read_only=True)
    try:
        return conn.execute("SELECT block_size FROM pragma_database_size()").fetchone()[0]
    finally:
        conn.close()


def test_plain_connect_creates_small_block_db(tmp_path):
    p = tmp_path / "plain.duckdb"
    conn = duckdb.connect(str(p))
    conn.execute("CREATE TABLE t (i INTEGER)")
    conn.close()
    assert _file_block_size(p) == 16384


def test_open_duckdb_funnel_inherits_small_blocks(tmp_path):
    """The production funnel creates test DBs small too — this is the path
    every per-test ``system.duckdb`` takes."""
    p = tmp_path / "funnel.duckdb"
    conn = _open_duckdb(str(p))
    conn.execute("CREATE TABLE t (i INTEGER)")
    conn.close()
    assert _file_block_size(p) == 16384


def test_explicit_config_still_wins(tmp_path):
    """A caller that asks for a specific block size is not overridden."""
    p = tmp_path / "explicit.duckdb"
    conn = duckdb.connect(str(p), config={"default_block_size": "262144"})
    conn.execute("CREATE TABLE t (i INTEGER)")
    conn.close()
    assert _file_block_size(p) == 262144


def test_default_block_file_still_opens_under_wrapper(tmp_path):
    """A DB created at DuckDB's 256 KiB default (e.g. shipped fixtures or
    production files) must open fine through the wrapper — the injected
    ``default_block_size`` only affects *created* databases."""
    p = tmp_path / "prodlike.duckdb"
    conn = duckdb.connect(str(p), config={"default_block_size": "262144"})
    conn.execute("CREATE TABLE t (i INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.close()

    reopened = duckdb.connect(str(p), read_only=True)
    try:
        assert reopened.execute("SELECT count(*) FROM t").fetchone()[0] == 1
        assert reopened.execute("SELECT block_size FROM pragma_database_size()").fetchone()[0] == 262144
    finally:
        reopened.close()
