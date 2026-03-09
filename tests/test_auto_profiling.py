"""Tests for auto-profiling: profile_changed_tables() function."""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import duckdb
import pytest

from src.profiler import (
    TableInfo,
    profile_changed_tables,
    PROFILES_OUTPUT_PATH,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_parquet(tmp_path: Path, folder: str, table_name: str) -> Path:
    """Create a small parquet file and return the file path."""
    folder_path = tmp_path / "parquet" / folder
    folder_path.mkdir(parents=True, exist_ok=True)
    parquet_path = folder_path / f"{table_name}.parquet"
    con = duckdb.connect()
    con.execute(f"""
        COPY (
            SELECT * FROM (VALUES
                (1, 'alpha', 10.0),
                (2, 'beta', 20.0),
                (3, 'gamma', 30.0)
            ) AS t(id, name, value)
        ) TO '{parquet_path}' (FORMAT PARQUET)
    """)
    con.close()
    return parquet_path


def _make_data_description(tmp_path: Path, tables: list[dict]) -> Path:
    """Create a minimal data_description.md with the given table definitions."""
    import yaml

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    dd_path = docs_dir / "data_description.md"

    table_defs = []
    for t in tables:
        table_defs.append({
            "id": t["id"],
            "name": t["name"],
            "description": t.get("description", f"Table {t['name']}"),
            "primary_key": t.get("primary_key", "id"),
            "sync_strategy": t.get("sync_strategy", "full"),
            "foreign_keys": [],
        })

    yaml_content = yaml.dump(
        {"tables": table_defs, "folder_mapping": t.get("folder_mapping", {})},
        default_flow_style=False,
    )
    dd_path.write_text(f"# Data\n\n```yaml\n{yaml_content}```\n")
    return dd_path


def _make_profiles_json(metadata_dir: Path, tables: dict) -> Path:
    """Write an existing profiles.json."""
    metadata_dir.mkdir(parents=True, exist_ok=True)
    profiles_path = metadata_dir / "profiles.json"
    profiles_path.write_text(json.dumps({
        "generated_at": "2026-01-01T00:00:00Z",
        "version": "1.0",
        "tables": tables,
    }))
    return profiles_path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def data_env(tmp_path):
    """Set up a temporary data environment with parquet + data_description.

    Returns a dict with paths and table definitions.
    """
    # Create two tables' parquet files
    _make_parquet(tmp_path, "bucket_a", "orders")
    _make_parquet(tmp_path, "bucket_a", "customers")

    # Create data_description.md
    folder_mapping = {"in.c-main": "bucket_a"}
    tables = [
        {
            "id": "in.c-main.orders",
            "name": "orders",
            "primary_key": "id",
            "sync_strategy": "full",
            "folder_mapping": folder_mapping,
        },
        {
            "id": "in.c-main.customers",
            "name": "customers",
            "primary_key": "id",
            "sync_strategy": "full",
            "folder_mapping": folder_mapping,
        },
    ]
    dd_path = _make_data_description(tmp_path, tables)

    metadata_dir = tmp_path / "parquet" / ".." / "metadata"
    metadata_dir = tmp_path / "metadata"
    metadata_dir.mkdir(parents=True, exist_ok=True)

    return {
        "tmp_path": tmp_path,
        "parquet_dir": tmp_path / "parquet",
        "metadata_dir": metadata_dir,
        "docs_dir": tmp_path / "docs",
        "dd_path": dd_path,
        "profiles_path": metadata_dir / "profiles.json",
    }


def _patch_profiler_paths(data_env):
    """Return a dict of patches for profiler module-level path constants."""
    return {
        "src.profiler.PARQUET_DIR": data_env["parquet_dir"],
        "src.profiler.METADATA_DIR": data_env["metadata_dir"],
        "src.profiler.PROFILES_OUTPUT_PATH": data_env["profiles_path"],
        "src.profiler.DATA_DESCRIPTION_PATH": data_env["dd_path"],
        "src.profiler.SYNC_STATE_PATH": data_env["metadata_dir"] / "sync_state.json",
        "src.profiler.METRICS_YML_PATH": data_env["docs_dir"] / "metrics.yml",
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class TestProfileChangedTablesReturnsCounts:
    """profile_changed_tables returns correct success/errors/skipped counts."""

    def test_all_tables_profiled(self, data_env):
        patches = _patch_profiler_paths(data_env)
        with patch.multiple("src.profiler", **{k.split(".")[-1]: v for k, v in patches.items()}):
            result = profile_changed_tables(["orders", "customers"])

        assert result["success"] == 2
        assert result["errors"] == 0
        assert result["skipped"] == 0

    def test_single_table_profiled(self, data_env):
        patches = _patch_profiler_paths(data_env)
        with patch.multiple("src.profiler", **{k.split(".")[-1]: v for k, v in patches.items()}):
            result = profile_changed_tables(["orders"])

        assert result["success"] == 1
        assert result["errors"] == 0
        assert result["skipped"] == 0

    def test_profiles_json_written(self, data_env):
        patches = _patch_profiler_paths(data_env)
        with patch.multiple("src.profiler", **{k.split(".")[-1]: v for k, v in patches.items()}):
            profile_changed_tables(["orders"])

        profiles_path = data_env["profiles_path"]
        assert profiles_path.exists()
        data = json.loads(profiles_path.read_text())
        assert "orders" in data["tables"]
        assert data["tables"]["orders"]["row_count"] == 3


class TestPreservesExistingProfiles:
    """When profiling a subset, existing profiles for other tables are preserved."""

    def test_existing_profiles_kept(self, data_env):
        # Write pre-existing profiles for a table called "legacy"
        _make_profiles_json(data_env["metadata_dir"], {
            "legacy": {"row_count": 999, "column_count": 5, "alerts": []},
        })

        patches = _patch_profiler_paths(data_env)
        with patch.multiple("src.profiler", **{k.split(".")[-1]: v for k, v in patches.items()}):
            result = profile_changed_tables(["orders"])

        assert result["success"] == 1

        data = json.loads(data_env["profiles_path"].read_text())
        # New profile written
        assert "orders" in data["tables"]
        # Old profile preserved
        assert "legacy" in data["tables"]
        assert data["tables"]["legacy"]["row_count"] == 999

    def test_existing_profile_overwritten_for_reprofiled_table(self, data_env):
        # Write stale profile for "orders"
        _make_profiles_json(data_env["metadata_dir"], {
            "orders": {"row_count": 0, "column_count": 0, "alerts": [], "_stale": True},
        })

        patches = _patch_profiler_paths(data_env)
        with patch.multiple("src.profiler", **{k.split(".")[-1]: v for k, v in patches.items()}):
            result = profile_changed_tables(["orders"])

        assert result["success"] == 1
        data = json.loads(data_env["profiles_path"].read_text())
        # Profile should be fresh, not the stale one
        assert data["tables"]["orders"]["row_count"] == 3
        assert "_stale" not in data["tables"]["orders"]


class TestErrorsCounted:
    """Errors during profiling are counted and don't abort the whole run."""

    def test_error_counted_not_aborted(self, data_env):
        # Capture the real profile_table before patching to avoid recursion
        from src.profiler import profile_table as real_profile_table

        def _failing_profile_table(table, *args, **kwargs):
            if table.name == "orders":
                raise RuntimeError("Simulated profiling error")
            return real_profile_table(table, *args, **kwargs)

        patches = _patch_profiler_paths(data_env)
        with patch.multiple("src.profiler", **{k.split(".")[-1]: v for k, v in patches.items()}), \
             patch("src.profiler.profile_table", side_effect=_failing_profile_table):
            result = profile_changed_tables(["orders", "customers"])

        assert result["errors"] == 1
        assert result["success"] == 1
        assert result["skipped"] == 0

    def test_all_errors(self, data_env):
        patches = _patch_profiler_paths(data_env)

        def _always_fail(table, *args, **kwargs):
            raise RuntimeError("Simulated error")

        with patch.multiple("src.profiler", **{k.split(".")[-1]: v for k, v in patches.items()}), \
             patch("src.profiler.profile_table", side_effect=_always_fail):
            result = profile_changed_tables(["orders", "customers"])

        assert result["errors"] == 2
        assert result["success"] == 0
        assert result["skipped"] == 0


class TestSkippedTables:
    """Tables without parquet files or not in data_description are skipped."""

    def test_unknown_table_skipped(self, data_env):
        patches = _patch_profiler_paths(data_env)
        with patch.multiple("src.profiler", **{k.split(".")[-1]: v for k, v in patches.items()}):
            result = profile_changed_tables(["nonexistent_table"])

        assert result["skipped"] == 1
        assert result["success"] == 0
        assert result["errors"] == 0

    def test_missing_parquet_skipped(self, data_env):
        # Add a table to data_description but don't create its parquet file
        import yaml

        dd_path = data_env["dd_path"]
        folder_mapping = {"in.c-main": "bucket_a"}
        tables = [
            {
                "id": "in.c-main.orders",
                "name": "orders",
                "description": "Orders table",
                "primary_key": "id",
                "sync_strategy": "full",
                "foreign_keys": [],
            },
            {
                "id": "in.c-main.no_data",
                "name": "no_data",
                "description": "Table without parquet",
                "primary_key": "id",
                "sync_strategy": "full",
                "foreign_keys": [],
            },
        ]
        yaml_content = yaml.dump(
            {"tables": tables, "folder_mapping": folder_mapping},
            default_flow_style=False,
        )
        dd_path.write_text(f"# Data\n\n```yaml\n{yaml_content}```\n")

        patches = _patch_profiler_paths(data_env)
        with patch.multiple("src.profiler", **{k.split(".")[-1]: v for k, v in patches.items()}):
            result = profile_changed_tables(["orders", "no_data"])

        assert result["success"] == 1
        assert result["skipped"] == 1
        assert result["errors"] == 0

    def test_empty_list(self, data_env):
        patches = _patch_profiler_paths(data_env)
        with patch.multiple("src.profiler", **{k.split(".")[-1]: v for k, v in patches.items()}):
            result = profile_changed_tables([])

        assert result["success"] == 0
        assert result["skipped"] == 0
        assert result["errors"] == 0

    def test_mixed_valid_invalid_unknown(self, data_env):
        """Combination: one valid, one unknown, one missing parquet."""
        import yaml

        dd_path = data_env["dd_path"]
        folder_mapping = {"in.c-main": "bucket_a"}
        tables = [
            {
                "id": "in.c-main.orders",
                "name": "orders",
                "description": "Orders table",
                "primary_key": "id",
                "sync_strategy": "full",
                "foreign_keys": [],
            },
            {
                "id": "in.c-main.ghost",
                "name": "ghost",
                "description": "Ghost table without data",
                "primary_key": "id",
                "sync_strategy": "full",
                "foreign_keys": [],
            },
        ]
        yaml_content = yaml.dump(
            {"tables": tables, "folder_mapping": folder_mapping},
            default_flow_style=False,
        )
        dd_path.write_text(f"# Data\n\n```yaml\n{yaml_content}```\n")

        patches = _patch_profiler_paths(data_env)
        with patch.multiple("src.profiler", **{k.split(".")[-1]: v for k, v in patches.items()}):
            # orders = valid, ghost = no parquet, unknown = not in data_description
            result = profile_changed_tables(["orders", "ghost", "unknown"])

        assert result["success"] == 1
        assert result["skipped"] == 2  # ghost (no parquet) + unknown (not in DD)
        assert result["errors"] == 0
