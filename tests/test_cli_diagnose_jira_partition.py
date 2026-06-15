"""Tests for the Jira partition-format detector (issue #394).

Covers both the low-level detector function and its integration into
`agnes diagnose` output.

Layouts under test:

- *flat* (old): ``data/{table}/YYYY-MM.parquet``  e.g. ``2025-01.parquet``
- *hive* (new): ``data/{table}/month=2025-01/part-0.parquet``
- *mixed*: some tables flat, some hive, or both layouts present in one
  table directory
- *absent*: no Jira data on disk at all
"""

import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from cli.lib.jira_partition_check import detect_jira_partition_layout
from cli.main import app

runner = CliRunner()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def jira_flat(tmp_path: Path) -> Path:
    """Flat YYYY-MM.parquet layout under tmp_path/data/issues/."""
    issues = tmp_path / "data" / "issues"
    issues.mkdir(parents=True)
    (issues / "2025-01.parquet").write_bytes(b"PAR1")
    (issues / "2025-02.parquet").write_bytes(b"PAR1")
    comments = tmp_path / "data" / "comments"
    comments.mkdir(parents=True)
    (comments / "2025-01.parquet").write_bytes(b"PAR1")
    return tmp_path


@pytest.fixture()
def jira_hive(tmp_path: Path) -> Path:
    """Hive month=*/ layout under tmp_path/data/issues/."""
    issues = tmp_path / "data" / "issues"
    (issues / "month=2025-01").mkdir(parents=True)
    (issues / "month=2025-01" / "part-0.parquet").write_bytes(b"PAR1")
    (issues / "month=2025-02").mkdir(parents=True)
    (issues / "month=2025-02" / "part-0.parquet").write_bytes(b"PAR1")
    comments = tmp_path / "data" / "comments"
    (comments / "month=2025-01").mkdir(parents=True)
    (comments / "month=2025-01" / "part-0.parquet").write_bytes(b"PAR1")
    return tmp_path


@pytest.fixture()
def jira_mixed(tmp_path: Path) -> Path:
    """Mixed: issues is flat, comments is hive."""
    issues = tmp_path / "data" / "issues"
    issues.mkdir(parents=True)
    (issues / "2025-01.parquet").write_bytes(b"PAR1")
    comments = tmp_path / "data" / "comments"
    (comments / "month=2025-01").mkdir(parents=True)
    (comments / "month=2025-01" / "part-0.parquet").write_bytes(b"PAR1")
    return tmp_path


@pytest.fixture()
def jira_absent(tmp_path: Path) -> Path:
    """No Jira data at all — data dir may not exist."""
    return tmp_path


# ---------------------------------------------------------------------------
# Unit tests for detect_jira_partition_layout
# ---------------------------------------------------------------------------


class TestDetectJiraPartitionLayout:
    def test_flat_returns_old(self, jira_flat: Path):
        result = detect_jira_partition_layout(jira_flat)
        assert result["layout"] == "old"
        assert result["status"] == "warning"

    def test_hive_returns_new(self, jira_hive: Path):
        result = detect_jira_partition_layout(jira_hive)
        assert result["layout"] == "new"
        assert result["status"] == "ok"

    def test_mixed_returns_mixed(self, jira_mixed: Path):
        result = detect_jira_partition_layout(jira_mixed)
        assert result["layout"] == "mixed"
        assert result["status"] == "warning"

    def test_absent_returns_info(self, jira_absent: Path):
        result = detect_jira_partition_layout(jira_absent)
        assert result["layout"] == "absent"
        assert result["status"] == "info"

    def test_result_has_name_field(self, jira_flat: Path):
        result = detect_jira_partition_layout(jira_flat)
        assert result["name"] == "jira-partition-format"

    def test_result_has_detail_field(self, jira_flat: Path):
        result = detect_jira_partition_layout(jira_flat)
        assert "detail" in result
        assert len(result["detail"]) > 0

    def test_hive_detail_mentions_hive(self, jira_hive: Path):
        result = detect_jira_partition_layout(jira_hive)
        detail = result["detail"].lower()
        assert "hive" in detail or "new" in detail or "month=" in detail

    def test_flat_detail_mentions_flat_or_old(self, jira_flat: Path):
        result = detect_jira_partition_layout(jira_flat)
        detail = result["detail"].lower()
        assert "flat" in detail or "old" in detail or "yyyy-mm" in detail

    def test_mixed_detail_mentions_mixed(self, jira_mixed: Path):
        result = detect_jira_partition_layout(jira_mixed)
        assert "mixed" in result["detail"].lower()

    def test_flat_layout_counts_flat_tables(self, jira_flat: Path):
        result = detect_jira_partition_layout(jira_flat)
        assert result.get("flat_tables", 0) >= 1

    def test_hive_layout_counts_hive_tables(self, jira_hive: Path):
        result = detect_jira_partition_layout(jira_hive)
        assert result.get("hive_tables", 0) >= 1

    def test_custom_data_subdir(self, tmp_path: Path):
        """Caller can pass a path directly to the data/ subdirectory."""
        data_dir = tmp_path / "data"
        issues = data_dir / "issues"
        issues.mkdir(parents=True)
        (issues / "2025-03.parquet").write_bytes(b"PAR1")
        result = detect_jira_partition_layout(tmp_path)
        assert result["layout"] == "old"


# ---------------------------------------------------------------------------
# Integration: agnes diagnose picks up the check
# ---------------------------------------------------------------------------


def _api_resp(json_data=None):
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = json_data if json_data is not None else {"status": "ok"}
    r.elapsed = MagicMock()
    r.elapsed.total_seconds.return_value = 0.01
    return r


MINIMAL_HEALTH = {"status": "ok", "services": {}}


class TestDiagnoseJiraPartitionIntegration:
    """agnes diagnose surfaces the jira-partition-format check."""

    def test_diagnose_text_includes_jira_partition_check(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "config"))
        (tmp_path / "config").mkdir()

        # flat layout — expect warning
        issues = tmp_path / "data" / "issues"
        issues.mkdir(parents=True)
        (issues / "2025-01.parquet").write_bytes(b"PAR1")

        with (
            patch("cli.commands.diagnose.api_get", return_value=_api_resp(MINIMAL_HEALTH)),
            patch(
                "cli.commands.diagnose.detect_jira_partition_layout",
                return_value={
                    "name": "jira-partition-format",
                    "status": "warning",
                    "layout": "old",
                    "detail": "flat YYYY-MM.parquet layout detected",
                    "flat_tables": 1,
                    "hive_tables": 0,
                    "audience": "operator",
                },
            ),
        ):
            result = runner.invoke(app, ["diagnose"])

        assert result.exit_code == 0
        assert "jira-partition-format" in result.output

    def test_diagnose_json_includes_jira_partition_check(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "config"))
        (tmp_path / "config").mkdir()

        check_payload = {
            "name": "jira-partition-format",
            "status": "ok",
            "layout": "new",
            "detail": "hive month=*/ layout",
            "flat_tables": 0,
            "hive_tables": 2,
            "audience": "operator",
        }
        with (
            patch("cli.commands.diagnose.api_get", return_value=_api_resp(MINIMAL_HEALTH)),
            patch(
                "cli.commands.diagnose.detect_jira_partition_layout",
                return_value=check_payload,
            ),
        ):
            result = runner.invoke(app, ["diagnose", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        jira_check = next(
            (c for c in data["checks"] if c["name"] == "jira-partition-format"), None
        )
        assert jira_check is not None
        assert jira_check["status"] == "ok"
        assert jira_check["layout"] == "new"

    def test_diagnose_flat_layout_warning_in_json(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "config"))
        (tmp_path / "config").mkdir()

        check_payload = {
            "name": "jira-partition-format",
            "status": "warning",
            "layout": "old",
            "detail": "flat YYYY-MM.parquet layout detected",
            "flat_tables": 2,
            "hive_tables": 0,
            "audience": "operator",
        }
        with (
            patch("cli.commands.diagnose.api_get", return_value=_api_resp(MINIMAL_HEALTH)),
            patch(
                "cli.commands.diagnose.detect_jira_partition_layout",
                return_value=check_payload,
            ),
        ):
            result = runner.invoke(app, ["diagnose", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        jira_check = next(
            (c for c in data["checks"] if c["name"] == "jira-partition-format"), None
        )
        assert jira_check is not None
        assert jira_check["status"] == "warning"
        assert jira_check["layout"] == "old"

    def test_absent_jira_shows_info_not_warning(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "config"))
        (tmp_path / "config").mkdir()

        check_payload = {
            "name": "jira-partition-format",
            "status": "info",
            "layout": "absent",
            "detail": "no Jira parquet data found",
            "flat_tables": 0,
            "hive_tables": 0,
            "audience": "operator",
        }
        with (
            patch("cli.commands.diagnose.api_get", return_value=_api_resp(MINIMAL_HEALTH)),
            patch(
                "cli.commands.diagnose.detect_jira_partition_layout",
                return_value=check_payload,
            ),
        ):
            result = runner.invoke(app, ["diagnose", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        jira_check = next(
            (c for c in data["checks"] if c["name"] == "jira-partition-format"), None
        )
        assert jira_check is not None
        assert jira_check["status"] == "info"
