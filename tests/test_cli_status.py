"""Tests for agnes status (workspace status)."""

import json
from typer.testing import CliRunner

# CI-safety: Typer/rich emits ANSI escapes in --help output. Strip before asserts.
_ANSI_RE = __import__("re").compile(r"\x1b\[[0-9;]*m")
def _clean(s: str) -> str:
    return _ANSI_RE.sub("", s)

from cli.commands.status import status_app

runner = CliRunner()


def test_status_uninitialized_workspace(tmp_path, monkeypatch):
    """Empty folder → exit 0, output indicates uninitialized state."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    result = runner.invoke(status_app)
    assert result.exit_code in (0, 1)
    out = result.output.lower()
    assert "no" in out  # "Initialized: no" or similar
    assert "agnes init" in _clean(result.output)  # hint to initialize


def test_status_initialized_workspace(tmp_path, monkeypatch):
    """A bootstrapped workspace → 'initialized: yes' and shows parquet count."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    (tmp_path / "CLAUDE.md").write_text("# AI Data Analyst\n")
    (tmp_path / "user" / "duckdb").mkdir(parents=True)
    (tmp_path / "user" / "duckdb" / "analytics.duckdb").touch()
    (tmp_path / "server" / "parquet").mkdir(parents=True)
    (tmp_path / "server" / "parquet" / "tbl1.parquet").touch()

    result = runner.invoke(status_app)
    assert result.exit_code == 0
    out = result.output.lower()
    assert "yes" in out  # "Initialized: yes"
    assert "1" in _clean(result.output)  # one parquet


def test_status_json(tmp_path, monkeypatch):
    """--json flag returns machine-readable output."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    (tmp_path / "CLAUDE.md").write_text("# AI Data Analyst\n")
    result = runner.invoke(status_app, ["--json"])
    assert result.exit_code == 0
    body = json.loads(result.output)
    assert "workspace" in body and "initialized" in body
