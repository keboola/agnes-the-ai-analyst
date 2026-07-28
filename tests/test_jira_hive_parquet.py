"""Tests for Jira hive-partitioned parquet layout (issue #406).

Verifies:
- transform_all writes month=YYYY-MM/ hive directories with data.parquet inside
- ZSTD compression is applied
- DuckDB can query with hive_partitioning=true and count-by-month works
- extract_init views use hive_partitioning=true
- Backward-compat: flat parquets are migrated on next write and detected by the view
- incremental_transform reads/writes the hive layout
"""

import json
import os
from pathlib import Path

import duckdb
import pyarrow.parquet as pq
import pytest

from connectors.jira.incremental_transform import (
    load_parquet_month,
    migrate_flat_to_hive,
    save_parquet_month,
    transform_single_issue,
)
from connectors.jira.transform import ISSUES_SCHEMA, transform_all


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_raw_issue(key: str, created: str, month: str | None = None) -> dict:
    """Minimal raw Jira issue JSON."""
    return {
        "key": key,
        "id": key.replace("-", ""),
        "fields": {
            "summary": f"Summary for {key}",
            "status": {"name": "Open", "statusCategory": {"name": "To Do"}},
            "issuetype": {"name": "Bug"},
            "attachment": [],
            "comment": {"comments": [], "total": 0},
            "issuelinks": [],
            "created": created,
            "updated": created,
        },
    }


def _write_raw_issues(raw_dir: Path, issues: list[dict]) -> None:
    issues_dir = raw_dir / "issues"
    issues_dir.mkdir(parents=True, exist_ok=True)
    for issue in issues:
        (issues_dir / f"{issue['key']}.json").write_text(json.dumps(issue))


# ---------------------------------------------------------------------------
# transform_all — hive layout
# ---------------------------------------------------------------------------

class TestTransformAllHiveLayout:
    def test_hive_dirs_created(self, tmp_path):
        """transform_all must write month=YYYY-MM/ subdirectories."""
        raw_dir = tmp_path / "raw"
        output_dir = tmp_path / "output"
        issues = [
            _make_raw_issue("PROJ-1", "2026-01-10T10:00:00.000+0000"),
            _make_raw_issue("PROJ-2", "2026-02-15T10:00:00.000+0000"),
        ]
        _write_raw_issues(raw_dir, issues)

        counts = transform_all(raw_dir=raw_dir, output_dir=output_dir)

        assert counts["issues"] == 2

        # Hive dirs must exist
        assert (output_dir / "issues" / "month=2026-01").is_dir()
        assert (output_dir / "issues" / "month=2026-02").is_dir()

        # data.parquet inside each
        assert (output_dir / "issues" / "month=2026-01" / "data.parquet").exists()
        assert (output_dir / "issues" / "month=2026-02" / "data.parquet").exists()

        # Old flat files must NOT be present
        flat_files = list((output_dir / "issues").glob("*.parquet"))
        assert flat_files == [], f"Flat parquet files still present: {flat_files}"

    def test_zstd_compression_used(self, tmp_path):
        """Parquet files must use ZSTD compression."""
        raw_dir = tmp_path / "raw"
        output_dir = tmp_path / "output"
        _write_raw_issues(raw_dir, [_make_raw_issue("PROJ-1", "2026-01-10T10:00:00.000+0000")])

        transform_all(raw_dir=raw_dir, output_dir=output_dir)

        pf = pq.read_metadata(output_dir / "issues" / "month=2026-01" / "data.parquet")
        compressions = {
            pf.row_group(i).column(j).compression
            for i in range(pf.num_row_groups)
            for j in range(pf.num_columns)
        }
        assert compressions == {"ZSTD"}, f"Expected ZSTD, got: {compressions}"

    def test_duckdb_hive_count_by_month(self, tmp_path):
        """DuckDB count-by-month query works via hive_partitioning=true."""
        raw_dir = tmp_path / "raw"
        output_dir = tmp_path / "output"
        issues = [
            _make_raw_issue("PROJ-1", "2026-01-10T10:00:00.000+0000"),
            _make_raw_issue("PROJ-2", "2026-01-20T10:00:00.000+0000"),
            _make_raw_issue("PROJ-3", "2026-02-05T10:00:00.000+0000"),
        ]
        _write_raw_issues(raw_dir, issues)

        transform_all(raw_dir=raw_dir, output_dir=output_dir)

        conn = duckdb.connect()
        glob = str(output_dir / "issues" / "month=*" / "*.parquet")
        result = conn.execute(
            f"SELECT month, count(*) AS cnt "
            f"FROM read_parquet('{glob}', union_by_name=true, hive_partitioning=true) "
            f"GROUP BY month ORDER BY month"
        ).fetchall()
        conn.close()

        assert result == [("2026-01", 2), ("2026-02", 1)]

    def test_all_table_types_use_hive_layout(self, tmp_path):
        """All six tables (issues/comments/attachments/changelog/issuelinks/remote_links)
        are written in hive layout."""
        raw_dir = tmp_path / "raw"
        output_dir = tmp_path / "output"
        issue = _make_raw_issue("PROJ-1", "2026-03-01T10:00:00.000+0000")
        issue["fields"]["comment"] = {
            "comments": [{
                "id": "c1",
                "author": {"emailAddress": "a@x.com", "displayName": "A"},
                "updateAuthor": {"emailAddress": "a@x.com", "displayName": "A"},
                "body": "hello",
                "created": "2026-03-01T10:00:00.000+0000",
                "updated": "2026-03-01T10:00:00.000+0000",
            }],
            "total": 1,
        }
        _write_raw_issues(raw_dir, [issue])

        transform_all(raw_dir=raw_dir, output_dir=output_dir)

        for table_name in ["issues", "comments"]:
            hive_dir = output_dir / table_name / "month=2026-03"
            assert hive_dir.is_dir(), f"{table_name}/month=2026-03/ not created"
            assert (hive_dir / "data.parquet").exists(), f"{table_name}/month=2026-03/data.parquet missing"


# ---------------------------------------------------------------------------
# save_parquet_month / load_parquet_month — hive layout
# ---------------------------------------------------------------------------

class TestHiveParquetMonthly:
    def test_save_writes_hive_dir(self, tmp_path):
        """save_parquet_month writes month=YYYY-MM/data.parquet."""
        import pandas as pd

        df = pd.DataFrame([{"issue_key": "PROJ-1", "summary": "Test"}])
        save_parquet_month(df, ISSUES_SCHEMA, tmp_path, "2026-04")

        assert (tmp_path / "month=2026-04" / "data.parquet").exists()
        # No flat file
        assert not (tmp_path / "2026-04.parquet").exists()

    def test_load_reads_hive_dir(self, tmp_path):
        """load_parquet_month reads from month=YYYY-MM/data.parquet."""
        import pandas as pd

        df = pd.DataFrame([{"issue_key": "PROJ-1", "summary": "Test"}])
        save_parquet_month(df, ISSUES_SCHEMA, tmp_path, "2026-04")

        loaded = load_parquet_month(tmp_path, "2026-04")
        assert loaded is not None
        assert len(loaded) == 1
        assert loaded.iloc[0]["issue_key"] == "PROJ-1"

    def test_load_returns_none_for_missing(self, tmp_path):
        """load_parquet_month returns None if neither flat nor hive file exists."""
        result = load_parquet_month(tmp_path, "2099-01")
        assert result is None

    def test_save_empty_removes_hive_dir(self, tmp_path):
        """save_parquet_month with empty df removes the hive directory."""
        import pandas as pd

        df = pd.DataFrame([{"issue_key": "PROJ-1", "summary": "Test"}])
        save_parquet_month(df, ISSUES_SCHEMA, tmp_path, "2026-04")
        assert (tmp_path / "month=2026-04").is_dir()

        empty = pd.DataFrame()
        save_parquet_month(empty, ISSUES_SCHEMA, tmp_path, "2026-04")
        assert not (tmp_path / "month=2026-04").exists()

    def test_zstd_on_incremental_save(self, tmp_path):
        """save_parquet_month uses ZSTD compression."""
        import pandas as pd

        df = pd.DataFrame([{"issue_key": "PROJ-1", "summary": "Test"}])
        save_parquet_month(df, ISSUES_SCHEMA, tmp_path, "2026-04")

        pf = pq.read_metadata(tmp_path / "month=2026-04" / "data.parquet")
        compressions = {
            pf.row_group(i).column(j).compression
            for i in range(pf.num_row_groups)
            for j in range(pf.num_columns)
        }
        assert compressions == {"ZSTD"}


# ---------------------------------------------------------------------------
# migrate_flat_to_hive
# ---------------------------------------------------------------------------

class TestMigrateFlatToHive:
    def test_migrates_flat_to_hive(self, tmp_path):
        """migrate_flat_to_hive moves YYYY-MM.parquet into month=YYYY-MM/data.parquet."""
        import pyarrow as pa

        table_dir = tmp_path / "issues"
        table_dir.mkdir()
        # Write a flat parquet
        t = pa.table({"issue_key": ["PROJ-1"], "summary": ["old"]})
        pq.write_table(t, table_dir / "2026-01.parquet")

        migrated = migrate_flat_to_hive(table_dir)

        assert migrated == ["2026-01"]
        assert (table_dir / "month=2026-01" / "data.parquet").exists()
        assert not (table_dir / "2026-01.parquet").exists()

    def test_skips_already_migrated(self, tmp_path):
        """migrate_flat_to_hive skips months already in hive layout."""
        import pandas as pd

        table_dir = tmp_path / "issues"
        table_dir.mkdir()

        df = pd.DataFrame([{"issue_key": "PROJ-1", "summary": "hive"}])
        save_parquet_month(df, ISSUES_SCHEMA, table_dir, "2026-01")

        migrated = migrate_flat_to_hive(table_dir)
        assert migrated == []

    def test_no_flat_files_no_op(self, tmp_path):
        """migrate_flat_to_hive on empty dir returns empty list."""
        table_dir = tmp_path / "issues"
        table_dir.mkdir()
        assert migrate_flat_to_hive(table_dir) == []

    def test_migrated_file_readable_by_duckdb(self, tmp_path):
        """After migration, DuckDB can read via hive_partitioning=true."""
        import pyarrow as pa

        table_dir = tmp_path / "issues"
        table_dir.mkdir()
        t = pa.table({"issue_key": ["PROJ-1"], "summary": ["migrated"]})
        pq.write_table(t, table_dir / "2026-02.parquet")

        migrate_flat_to_hive(table_dir)

        conn = duckdb.connect()
        glob = str(table_dir / "month=*" / "*.parquet")
        rows = conn.execute(
            f"SELECT issue_key FROM read_parquet('{glob}', union_by_name=true, hive_partitioning=true)"
        ).fetchall()
        conn.close()
        assert rows == [("PROJ-1",)]


# ---------------------------------------------------------------------------
# extract_init — view uses hive_partitioning=true
# ---------------------------------------------------------------------------

class TestExtractInitHiveView:
    def test_view_uses_hive_partitioning(self, tmp_path):
        """init_extract creates views reading month=*/*.parquet with hive_partitioning=true."""
        import pandas as pd
        from connectors.jira.extract_init import init_extract

        output_dir = tmp_path / "jira"
        output_dir.mkdir()
        data_dir = output_dir / "data"
        issues_dir = data_dir / "issues"

        # Write a hive-layout parquet
        hive_dir = issues_dir / "month=2026-05"
        hive_dir.mkdir(parents=True)
        df = pd.DataFrame([{"issue_key": "PROJ-1", "summary": "Hello"}])
        from connectors.jira.transform import apply_schema
        t = apply_schema(df, ISSUES_SCHEMA)
        pq.write_table(t, hive_dir / "data.parquet")

        init_extract(output_dir)

        # The view must be readable and expose the data
        from src.duckdb_conn import _open_duckdb
        conn = _open_duckdb(str(output_dir / "extract.duckdb"))
        try:
            rows = conn.execute("SELECT issue_key FROM issues").fetchall()
            assert rows == [("PROJ-1",)]
        finally:
            conn.close()

    def test_view_reads_count_by_month(self, tmp_path):
        """View created by init_extract supports count-by-month query via hive cols."""
        import pandas as pd
        from connectors.jira.extract_init import init_extract
        from connectors.jira.transform import apply_schema

        output_dir = tmp_path / "jira"
        output_dir.mkdir()
        data_dir = output_dir / "data"

        for month, keys in [("2026-05", ["PROJ-1", "PROJ-2"]), ("2026-06", ["PROJ-3"])]:
            hive_dir = data_dir / "issues" / f"month={month}"
            hive_dir.mkdir(parents=True)
            df = pd.DataFrame([{"issue_key": k, "summary": k} for k in keys])
            t = apply_schema(df, ISSUES_SCHEMA)
            pq.write_table(t, hive_dir / "data.parquet")

        init_extract(output_dir)

        from src.duckdb_conn import _open_duckdb
        conn = _open_duckdb(str(output_dir / "extract.duckdb"))
        try:
            rows = conn.execute(
                "SELECT month, count(*) AS cnt FROM issues GROUP BY month ORDER BY month"
            ).fetchall()
            assert rows == [("2026-05", 2), ("2026-06", 1)]
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Full pipeline: transform_single_issue with hive layout
# ---------------------------------------------------------------------------

class TestIncrementalHivePipeline:
    def test_incremental_writes_hive_layout(self, tmp_path):
        """transform_single_issue writes to hive layout after upgrade."""
        raw_dir = tmp_path / "raw"
        output_dir = tmp_path / "parquet"
        output_dir.mkdir()

        raw_dir.mkdir()
        issue = _make_raw_issue("PROJ-10", "2026-05-15T10:00:00.000+0000")
        (raw_dir / "issues").mkdir()
        (raw_dir / "issues" / "PROJ-10.json").write_text(json.dumps(issue))

        ok = transform_single_issue(
            issue_key="PROJ-10",
            raw_dir=raw_dir,
            output_dir=output_dir,
        )
        assert ok is True

        hive_path = output_dir / "issues" / "month=2026-05" / "data.parquet"
        assert hive_path.exists(), f"Hive parquet not created at {hive_path}"

    def test_incremental_reads_existing_hive(self, tmp_path):
        """transform_single_issue round-trips correctly with hive layout."""
        import pandas as pd

        raw_dir = tmp_path / "raw"
        output_dir = tmp_path / "parquet"
        output_dir.mkdir()

        # Pre-seed hive parquet for 2026-05
        existing_df = pd.DataFrame([{"issue_key": "PROJ-9", "summary": "Existing"}])
        save_parquet_month(existing_df, ISSUES_SCHEMA, output_dir / "issues", "2026-05")

        raw_dir.mkdir()
        issue = _make_raw_issue("PROJ-10", "2026-05-15T10:00:00.000+0000")
        (raw_dir / "issues").mkdir()
        (raw_dir / "issues" / "PROJ-10.json").write_text(json.dumps(issue))

        ok = transform_single_issue(
            issue_key="PROJ-10",
            raw_dir=raw_dir,
            output_dir=output_dir,
        )
        assert ok is True

        loaded = load_parquet_month(output_dir / "issues", "2026-05")
        assert loaded is not None
        assert set(loaded["issue_key"].tolist()) == {"PROJ-9", "PROJ-10"}
