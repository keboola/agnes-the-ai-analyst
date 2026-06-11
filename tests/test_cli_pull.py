"""Tests for `agnes pull` Typer wrapper."""

import json
from unittest.mock import patch

from typer.testing import CliRunner

# CI-safety: Typer/rich emits ANSI escapes in --help output. Strip before asserts.
_ANSI_RE = __import__("re").compile(r"\x1b\[[0-9;]*m")
def _clean(s: str) -> str:
    return _ANSI_RE.sub("", s)

from cli.commands.pull import pull_app

runner = CliRunner()


class _FakePullResult:
    """Minimal duck-typed PullResult so the legacy-hook nudge tests don't
    depend on a live server / real manifest."""
    tables_updated = 0
    parquets_total = 0
    rules_count = 0
    duration_s = 0.0
    errors: list = []
    stack_sync = None
    # Added in #594 (data-package prune): the human-readable pull summary
    # reads `result.tables_removed`, so the fake must carry the field too.
    tables_removed = 0


def _write_legacy_settings(workspace):
    sp = workspace / ".claude" / "settings.json"
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps({
        "hooks": {
            "SessionEnd": [
                {"hooks": [{"type": "command",
                            "command": "python server/scripts/collect_session.py"}]},
            ],
        }
    }), encoding="utf-8")


_NUDGE = "outdated hook layout"


def _run_pull_in(workspace, monkeypatch, args):
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(workspace))
    monkeypatch.setenv("AGNES_SERVER", "http://server.test:8000")
    monkeypatch.setenv("AGNES_TOKEN", "tok")
    with patch("cli.commands.pull.run_pull", return_value=_FakePullResult()):
        return runner.invoke(pull_app, args)


def test_pull_nudges_on_legacy_hooks(tmp_path, monkeypatch):
    """A legacy-hook workspace gets exactly one stderr nudge pointing at
    `agnes init`."""
    _write_legacy_settings(tmp_path)
    result = _run_pull_in(tmp_path, monkeypatch, [])
    assert result.exit_code == 0
    err = _clean(result.stderr or "")
    assert _NUDGE in err
    assert "agnes init" in err
    # Emitted exactly once.
    assert err.count(_NUDGE) == 1


def test_pull_no_nudge_on_modern_workspace(tmp_path, monkeypatch):
    """A modern `agnes init` workspace gets no nudge (no double-nudge)."""
    from cli.lib.hooks import install_claude_hooks
    install_claude_hooks(tmp_path)
    result = _run_pull_in(tmp_path, monkeypatch, [])
    assert result.exit_code == 0
    assert _NUDGE not in _clean(result.stderr or "")


def test_pull_nudge_suppressed_under_quiet(tmp_path, monkeypatch):
    """`--quiet` (the SessionStart hook path) stays silent — no nudge."""
    _write_legacy_settings(tmp_path)
    result = _run_pull_in(tmp_path, monkeypatch, ["--quiet"])
    assert result.exit_code == 0
    assert _NUDGE not in _clean(result.stderr or "")


def test_pull_help():
    result = runner.invoke(pull_app, ["--help"])
    assert result.exit_code == 0
    assert "--quiet" in _clean(result.output)
    assert "--json" in _clean(result.output)
    assert "--dry-run" in _clean(result.output)


def test_pull_no_server_friendly_exit(tmp_path, monkeypatch):
    """No configured server -> exit 1 with friendly hint (no traceback)."""
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_cfg"))
    monkeypatch.delenv("AGNES_SERVER", raising=False)
    monkeypatch.delenv("AGNES_TOKEN", raising=False)
    result = runner.invoke(pull_app, [])
    # Either exit 1 with hint, or exit 0 if a default server URL applies.
    # Either way, there must be no Python traceback in stderr/stdout.
    assert "Traceback" not in (_clean(result.output) + _clean(result.stderr or ''))
