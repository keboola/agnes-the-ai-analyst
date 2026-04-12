"""Tests for incremental Jira parquet transform (upsert_dataframe and friends)."""

from pathlib import Path
from unittest.mock import patch

import duckdb
import pandas as pd
import pytest

from connectors.jira.incremental_transform import (
    load_parquet_month,
    save_parquet_month,
    upsert_dataframe,
)


# Minimal schema compatible with ISSUES_SCHEMA for testing purposes
_SIMPLE_SCHEMA = {
    "issue_key": "string",
    "summary": "string",
}


@pytest.fixture
def parquet_dir(tmp_path):
    d = tmp_path / "parquet_data"
    d.mkdir()
    return d


def _make_df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


class TestUpsertDataframe:
    def test_insert_into_empty(self):
        """Upserting into None/empty creates a new DataFrame."""
        new_records = [{"issue_key": "PROJ-1", "summary": "Bug A"}]
        result = upsert_dataframe(None, new_records, "issue_key", "PROJ-1")
        assert len(result) == 1
        assert result.iloc[0]["issue_key"] == "PROJ-1"

    def test_insert_new_issue(self):
        """Upserting a new issue_key adds a new row."""
        existing = _make_df([{"issue_key": "PROJ-1", "summary": "Existing"}])
        new_records = [{"issue_key": "PROJ-2", "summary": "New issue"}]
        result = upsert_dataframe(existing, new_records, "issue_key", "PROJ-2")
        assert len(result) == 2
        keys = set(result["issue_key"].tolist())
        assert keys == {"PROJ-1", "PROJ-2"}

    def test_update_existing_issue(self):
        """Upserting an existing issue_key replaces the old row."""
        existing = _make_df([
            {"issue_key": "PROJ-1", "summary": "Old summary"},
            {"issue_key": "PROJ-2", "summary": "Other issue"},
        ])
        new_records = [{"issue_key": "PROJ-1", "summary": "Updated summary"}]
        result = upsert_dataframe(existing, new_records, "issue_key", "PROJ-1")
        assert len(result) == 2
        proj1 = result[result["issue_key"] == "PROJ-1"]
        assert proj1.iloc[0]["summary"] == "Updated summary"

    def test_delete_issue(self):
        """Upserting with empty records removes the issue (deletion case)."""
        existing = _make_df([
            {"issue_key": "PROJ-1", "summary": "To be deleted"},
            {"issue_key": "PROJ-2", "summary": "Keep this"},
        ])
        result = upsert_dataframe(existing, [], "issue_key", "PROJ-1")
        assert len(result) == 1
        assert result.iloc[0]["issue_key"] == "PROJ-2"

    def test_upsert_empty_existing_df(self):
        """Upserting into an empty (non-None) DataFrame works correctly."""
        existing = pd.DataFrame(columns=["issue_key", "summary"])
        new_records = [{"issue_key": "PROJ-5", "summary": "First issue"}]
        result = upsert_dataframe(existing, new_records, "issue_key", "PROJ-5")
        assert len(result) == 1
        assert result.iloc[0]["issue_key"] == "PROJ-5"

    def test_upsert_multiple_records_same_issue(self):
        """Multiple records for the same issue_key are all replaced."""
        existing = _make_df([
            {"issue_key": "PROJ-1", "summary": "Comment 1"},
            {"issue_key": "PROJ-1", "summary": "Comment 2"},
            {"issue_key": "PROJ-2", "summary": "Other"},
        ])
        new_records = [{"issue_key": "PROJ-1", "summary": "Updated comment"}]
        result = upsert_dataframe(existing, new_records, "issue_key", "PROJ-1")
        proj1_rows = result[result["issue_key"] == "PROJ-1"]
        assert len(proj1_rows) == 1  # Only the updated record
        assert proj1_rows.iloc[0]["summary"] == "Updated comment"


class TestParquetMonthlyPartitioning:
    def test_save_and_load_parquet(self, parquet_dir):
        """save_parquet_month writes and load_parquet_month reads correctly."""
        df = _make_df([
            {"issue_key": "PROJ-1", "summary": "Test issue"},
        ])
        save_parquet_month(df, _SIMPLE_SCHEMA, parquet_dir, "2026-04")
        loaded = load_parquet_month(parquet_dir, "2026-04")
        assert loaded is not None
        assert len(loaded) == 1
        assert loaded.iloc[0]["issue_key"] == "PROJ-1"

    def test_load_nonexistent_returns_none(self, parquet_dir):
        """load_parquet_month returns None if the file doesn't exist."""
        result = load_parquet_month(parquet_dir, "2099-01")
        assert result is None

    def test_save_empty_df_removes_file(self, parquet_dir):
        """save_parquet_month with empty df removes existing parquet file."""
        # First write a file
        df = _make_df([{"issue_key": "PROJ-1", "summary": "Test"}])
        save_parquet_month(df, _SIMPLE_SCHEMA, parquet_dir, "2026-01")
        assert (parquet_dir / "2026-01.parquet").exists()

        # Save empty df — file should be removed
        empty = pd.DataFrame()
        save_parquet_month(empty, _SIMPLE_SCHEMA, parquet_dir, "2026-01")
        assert not (parquet_dir / "2026-01.parquet").exists()

    def test_separate_months_independent_files(self, parquet_dir):
        """Different month_keys write to separate parquet files."""
        df_april = _make_df([{"issue_key": "PROJ-A", "summary": "April issue"}])
        df_may = _make_df([{"issue_key": "PROJ-B", "summary": "May issue"}])

        save_parquet_month(df_april, _SIMPLE_SCHEMA, parquet_dir, "2026-04")
        save_parquet_month(df_may, _SIMPLE_SCHEMA, parquet_dir, "2026-05")

        assert (parquet_dir / "2026-04.parquet").exists()
        assert (parquet_dir / "2026-05.parquet").exists()

        april_loaded = load_parquet_month(parquet_dir, "2026-04")
        may_loaded = load_parquet_month(parquet_dir, "2026-05")

        assert april_loaded.iloc[0]["issue_key"] == "PROJ-A"
        assert may_loaded.iloc[0]["issue_key"] == "PROJ-B"

    def test_parquet_readable_by_duckdb(self, parquet_dir):
        """Parquet files written by save_parquet_month are readable by DuckDB."""
        df = _make_df([
            {"issue_key": "PROJ-1", "summary": "DuckDB readable"},
            {"issue_key": "PROJ-2", "summary": "Also readable"},
        ])
        save_parquet_month(df, _SIMPLE_SCHEMA, parquet_dir, "2026-04")

        pq_file = str(parquet_dir / "2026-04.parquet")
        conn = duckdb.connect()
        rows = conn.execute(f"SELECT count(*) FROM read_parquet('{pq_file}')").fetchone()
        conn.close()
        assert rows[0] == 2

    def test_upsert_round_trip_with_real_parquet(self, parquet_dir):
        """Full upsert round trip: write, load, upsert, save, verify."""
        # Initial write
        initial = _make_df([
            {"issue_key": "PROJ-1", "summary": "Original"},
            {"issue_key": "PROJ-2", "summary": "Keep"},
        ])
        save_parquet_month(initial, _SIMPLE_SCHEMA, parquet_dir, "2026-04")

        # Load existing
        existing = load_parquet_month(parquet_dir, "2026-04")

        # Upsert update for PROJ-1
        updated = upsert_dataframe(
            existing,
            [{"issue_key": "PROJ-1", "summary": "Updated"}],
            "issue_key",
            "PROJ-1",
        )

        # Save back
        save_parquet_month(updated, _SIMPLE_SCHEMA, parquet_dir, "2026-04")

        # Reload and verify
        final = load_parquet_month(parquet_dir, "2026-04")
        assert len(final) == 2
        proj1 = final[final["issue_key"] == "PROJ-1"]
        assert proj1.iloc[0]["summary"] == "Updated"
        proj2 = final[final["issue_key"] == "PROJ-2"]
        assert proj2.iloc[0]["summary"] == "Keep"
