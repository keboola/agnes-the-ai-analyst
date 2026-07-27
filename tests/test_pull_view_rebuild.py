"""`_rebuild_duckdb_views` must build ONE view per partitioned table
(directory of parts) over a hive glob, while single-file tables keep their
stem view. Real parquet parts, so read_parquet actually resolves them.
"""
from __future__ import annotations

import duckdb

from cli.lib.pull import _rebuild_duckdb_views


def _write_parquet(path, n):
    path.parent.mkdir(parents=True, exist_ok=True)
    c = duckdb.connect()
    try:
        c.execute(f"COPY (SELECT range AS id FROM range({n})) TO '{path}' (FORMAT PARQUET)")
    finally:
        c.close()


def _analytics(workspace):
    return duckdb.connect(str(workspace / "user" / "duckdb" / "analytics.duckdb"))


def test_partitioned_dir_builds_one_hive_view(tmp_path):
    pq = tmp_path / "server" / "parquet"
    _write_parquet(pq / "issues" / "month=2026-06" / "data.parquet", 3)
    _write_parquet(pq / "issues" / "month=2026-07" / "data.parquet", 2)

    _rebuild_duckdb_views(tmp_path, pq)

    conn = _analytics(tmp_path)
    try:
        assert conn.execute("SELECT count(*) FROM issues").fetchone()[0] == 5
        # hive_partitioning surfaces `month` from the dir names
        assert conn.execute("SELECT count(DISTINCT month) FROM issues").fetchone()[0] == 2
    finally:
        conn.close()


def test_single_file_table_still_builds_stem_view(tmp_path):
    pq = tmp_path / "server" / "parquet"
    _write_parquet(pq / "account.parquet", 5)

    _rebuild_duckdb_views(tmp_path, pq)

    conn = _analytics(tmp_path)
    try:
        assert conn.execute("SELECT count(*) FROM account").fetchone()[0] == 5
    finally:
        conn.close()


def test_mixed_single_and_partitioned(tmp_path):
    pq = tmp_path / "server" / "parquet"
    _write_parquet(pq / "account.parquet", 4)
    _write_parquet(pq / "issues" / "month=2026-06" / "data.parquet", 7)

    _rebuild_duckdb_views(tmp_path, pq)

    conn = _analytics(tmp_path)
    try:
        assert conn.execute("SELECT count(*) FROM account").fetchone()[0] == 4
        assert conn.execute("SELECT count(*) FROM issues").fetchone()[0] == 7
    finally:
        conn.close()


def test_staging_dir_is_ignored(tmp_path):
    """A leftover .staging-* dir from an interrupted sync must not become a view."""
    pq = tmp_path / "server" / "parquet"
    _write_parquet(pq / ".staging-issues" / "month=2026-06" / "data.parquet", 3)
    _write_parquet(pq / "account.parquet", 1)

    _rebuild_duckdb_views(tmp_path, pq)

    conn = _analytics(tmp_path)
    try:
        names = {r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_type='VIEW'").fetchall()}
        assert "account" in names
        assert not any(n.startswith(".staging") for n in names)
    finally:
        conn.close()


def test_flat_partitioned_layout_builds_queryable_view(tmp_path):
    """Keboola flat-partitioned layout ({key}.parquet, NO key=value dirs) must
    build a queryable view — hive_partitioning=true must not error on it
    (Devin re-review: flat layout untested)."""
    pq = tmp_path / "server" / "parquet"
    _write_parquet(pq / "cost" / "2025_11.parquet", 4)
    _write_parquet(pq / "cost" / "2025_12.parquet", 6)

    _rebuild_duckdb_views(tmp_path, pq)

    conn = _analytics(tmp_path)
    try:
        assert conn.execute("SELECT count(*) FROM cost").fetchone()[0] == 10
    finally:
        conn.close()
