"""`agnes pull` v49 per-type status block (Task 8.12 of v49 plan).

Verifies that the CLI surface renders the ``SyncReport`` produced by
``cli/lib/pull_sync.py`` so the operator sees per-type added / updated /
removed counts instead of just the legacy aggregate "Updated N tables"
line. Older servers (no `stack_sync` on `PullResult`) keep the legacy
output untouched.
"""

from __future__ import annotations

import re

from typer.testing import CliRunner

from cli.commands.pull import pull_app
from cli.lib.pull import PullResult
from cli.lib.pull_sync import SyncReport, TypeReport


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _clean(s: str) -> str:
    return _ANSI_RE.sub("", s)


runner = CliRunner()


def _mock_pull_result(stack_sync) -> PullResult:
    """Construct a `PullResult` whose `stack_sync` is `stack_sync`."""
    return PullResult(
        tables_updated=0,
        parquets_total=0,
        rules_count=0,
        duration_s=0.01,
        errors=[],
        stack_sync=stack_sync,
    )


def _patch_run_pull(monkeypatch, result: PullResult) -> None:
    """Stub `cli.commands.pull.run_pull` so the Typer wrapper executes
    its rendering code path without touching the network. We patch the
    module-local binding (not the source module) because Python rebinds
    at import time."""
    import cli.commands.pull as pull_mod

    def fake(server_url, token, workspace, *, dry_run=False, skip_materialize=False, show_progress=False):
        return result

    monkeypatch.setattr(pull_mod, "run_pull", fake)
    monkeypatch.setenv("AGNES_SERVER", "http://localhost:0")
    monkeypatch.setenv("AGNES_TOKEN", "dummy")


def test_pull_emits_per_type_status_block(monkeypatch, tmp_path):
    """All three types changed — block lists added/updated/removed per type."""
    report = SyncReport(
        direct_tables=TypeReport(added=1, updated=0, removed=0),
        data_packages=TypeReport(added=2, updated=1, removed=0),
        memory_domains=TypeReport(added=0, updated=0, removed=1),
    )
    _patch_run_pull(monkeypatch, _mock_pull_result(report))
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    result = runner.invoke(pull_app, [])
    out = _clean(result.output)
    assert result.exit_code == 0, out
    assert "Stack sync:" in out
    assert "direct_tables:" in out
    assert "1 added" in out
    assert "data_packages:" in out
    assert "2 added" in out
    assert "1 updated" in out
    assert "memory_domains:" in out
    assert "1 removed" in out


def test_pull_idempotent_renders_zero_changes(monkeypatch, tmp_path):
    """No deltas anywhere — each line shows the ✓ 0 changes idle state."""
    report = SyncReport()  # all zeros
    _patch_run_pull(monkeypatch, _mock_pull_result(report))
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    result = runner.invoke(pull_app, [])
    out = _clean(result.output)
    assert result.exit_code == 0, out
    assert "Stack sync:" in out
    # Three "0 changes" rows.
    assert out.count("0 changes") == 3


def test_pull_invariant_violations_surface_as_warning(monkeypatch, tmp_path):
    """Stack invariants flagged by audit emit a `warn:` line. We
    don't care which stream (stdout vs stderr) — the mixed output
    captured by CliRunner is enough to verify the line was emitted."""
    report = SyncReport()
    report.invariant_violations = ["orphan _shared parquet: abc.parquet"]
    _patch_run_pull(monkeypatch, _mock_pull_result(report))
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    result = runner.invoke(pull_app, [])
    out = _clean(result.output)
    assert result.exit_code == 0, out
    assert "stack invariant violation" in out


def test_pull_legacy_result_without_stack_sync_no_block(monkeypatch, tmp_path):
    """Older server returns a PullResult with stack_sync=None — the
    new block is silently skipped so legacy output is byte-for-byte
    unchanged for users who haven't upgraded the server yet."""
    _patch_run_pull(monkeypatch, _mock_pull_result(None))
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    result = runner.invoke(pull_app, [])
    out = _clean(result.output)
    assert result.exit_code == 0
    assert "Stack sync:" not in out


def test_pull_quiet_suppresses_status_block(monkeypatch, tmp_path):
    """--quiet (SessionStart hook mode) skips success stdout entirely,
    including the new status block."""
    report = SyncReport(
        direct_tables=TypeReport(added=1),
    )
    _patch_run_pull(monkeypatch, _mock_pull_result(report))
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
    result = runner.invoke(pull_app, ["--quiet"])
    out = _clean(result.output)
    assert result.exit_code == 0
    assert "Stack sync:" not in out
