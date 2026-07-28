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


def test_status_initialized_via_legacy_claude_md_marker(tmp_path, monkeypatch):
    """Pre-#259 workspaces have only the legacy `# AI Data Analyst` string
    in CLAUDE.md (no `.claude/init-complete` sentinel) — keep recognising
    them so older analyst checkouts don't flip to 'Initialized: no' after
    a CLI upgrade."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    (tmp_path / "CLAUDE.md").write_text("# AI Data Analyst\n")
    (tmp_path / "user" / "duckdb").mkdir(parents=True)
    (tmp_path / "user" / "duckdb" / "analytics.duckdb").touch()
    (tmp_path / "server" / "parquet").mkdir(parents=True)
    (tmp_path / "server" / "parquet" / "tbl1.parquet").touch()

    result = runner.invoke(status_app)
    assert result.exit_code == 0
    out = result.output.lower()
    assert "yes" in out
    assert "1" in _clean(result.output)


def test_status_initialized_via_init_complete_sentinel(tmp_path, monkeypatch):
    """Override-mode workspaces (customer-supplied Initial-Workspace
    templates whose CLAUDE.md body legitimately omits the literal
    'AI Data Analyst' substring) must still report 'Initialized: yes'
    when `.claude/init-complete` exists. The sentinel is authoritative
    for override mode; the legacy CLAUDE.md grep is fallback-only."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    # Heading deliberately omits the canonical "AI Data Analyst" marker;
    # this is what a custom override template can look like in the wild.
    (tmp_path / "CLAUDE.md").write_text("# Acme — Custom Workspace\n")
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "init-complete").write_text(
        "completed_at: 2026-05-26T11:33:30Z\nagnes_version: 0.55.13\noverride: true\n"
    )
    result = runner.invoke(status_app)
    assert result.exit_code == 0
    assert "yes" in result.output.lower()
    assert "agnes init" not in _clean(result.output)


def test_status_json(tmp_path, monkeypatch):
    """--json flag returns machine-readable output."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "init-complete").write_text("agnes_version: 0.55.13\n")
    result = runner.invoke(status_app, ["--json"])
    assert result.exit_code == 0
    body = json.loads(result.output)
    assert "workspace" in body and "initialized" in body
    assert body["initialized"] is True
