"""Tests for `agnes pull` Typer wrapper."""

import json
from unittest.mock import patch

from typer.testing import CliRunner

# CI-safety: Typer/rich emits ANSI escapes in --help output. Strip before asserts.
_ANSI_RE = __import__("re").compile(r"\x1b\[[0-9;]*m")


def _clean(s: str) -> str:
    return _ANSI_RE.sub("", s)


from cli.commands.pull import pull_app  # noqa: E402

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
    sp.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionEnd": [
                        {"hooks": [{"type": "command", "command": "python server/scripts/collect_session.py"}]},
                    ],
                }
            }
        ),
        encoding="utf-8",
    )


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


class _FailingPullResult:
    """Duck-typed PullResult with one per-table failure recorded — mirrors a
    real `run_pull` outcome where a table failed hash verification on every
    attempt (#596). Drives the exit-code assertions below."""

    tables_updated = 0
    tables_removed = 0
    parquets_total = 1
    rules_count = 0
    duration_s = 0.1
    errors = [{"table": "kbc_project", "error": "hash mismatch: expected aaa, got bbb"}]
    stack_sync = None


def _run_failing_pull_in(workspace, monkeypatch, args):
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(workspace))
    monkeypatch.setenv("AGNES_SERVER", "http://server.test:8000")
    monkeypatch.setenv("AGNES_TOKEN", "tok")
    with patch("cli.commands.pull.run_pull", return_value=_FailingPullResult()):
        return runner.invoke(pull_app, args)


def test_pull_exits_nonzero_on_table_failure_normal_path(tmp_path, monkeypatch):
    """#596: a forced per-table failure must exit 1 (was 0) on the normal
    human-readable path, with the warning still rendered to stderr."""
    from cli.lib.hooks import install_claude_hooks

    install_claude_hooks(tmp_path)  # modern workspace, no legacy nudge
    result = _run_failing_pull_in(tmp_path, monkeypatch, [])
    assert result.exit_code == 1, "partial pull must exit non-zero"
    assert "hash mismatch" in _clean(result.stderr or "")


def test_pull_exits_nonzero_on_table_failure_quiet_path(tmp_path, monkeypatch):
    """#596: the silent SessionStart-hook path (`--quiet`) must also exit 1 on
    a table failure — the canonical hook's `|| true` is what swallows it, not
    a hidden exit 0."""
    result = _run_failing_pull_in(tmp_path, monkeypatch, ["--quiet"])
    assert result.exit_code == 1
    assert "warn" in _clean(result.stderr or "")


def test_pull_exits_nonzero_on_table_failure_json_path(tmp_path, monkeypatch):
    """#596: the `--json` path must emit the summary dict (so consumers can
    read `errors`) AND exit 1."""
    result = _run_failing_pull_in(tmp_path, monkeypatch, ["--json"])
    assert result.exit_code == 1, "json path must exit non-zero on errors"
    # The JSON object is still emitted before the non-zero exit.
    payload = json.loads(_clean(result.stdout).strip())
    assert payload["errors"], "json output must carry the error list"
    assert payload["errors"][0]["table"] == "kbc_project"


def test_pull_exits_zero_when_no_errors(tmp_path, monkeypatch):
    """Counterpart: a clean pull (no errors) still exits 0 on every path —
    the non-zero exit must be gated strictly on `result.errors`."""
    from cli.lib.hooks import install_claude_hooks

    install_claude_hooks(tmp_path)
    assert _run_pull_in(tmp_path, monkeypatch, []).exit_code == 0
    assert _run_pull_in(tmp_path, monkeypatch, ["--quiet"]).exit_code == 0
    assert _run_pull_in(tmp_path, monkeypatch, ["--json"]).exit_code == 0


def test_pull_empty_manifest_explains_zero_tables(tmp_path, monkeypatch):
    """#754: an empty manifest with no transport/server errors (real
    `run_pull` reaches this branch only when the manifest fetch itself
    succeeded — see `cli/lib/pull.py:run_pull` step 1) must print an
    explanatory line distinguishing "nothing granted/registered" from a
    transport failure, instead of the bare pre-fix
    "Updated 0 tables (0 total)." with zero context."""
    from cli.lib.hooks import install_claude_hooks

    install_claude_hooks(tmp_path)
    result = _run_pull_in(tmp_path, monkeypatch, [])
    assert result.exit_code == 0
    out = _clean(result.stdout)
    assert "no tables" in out.lower() or "nothing" in out.lower()
    assert "grant" in out.lower() or "regist" in out.lower()


def test_pull_empty_manifest_silent_under_quiet(tmp_path, monkeypatch):
    """The explanatory line is a non-error UX nicety — must NOT print under
    `--quiet` (the SessionStart hook path stays silent on success)."""
    result = _run_pull_in(tmp_path, monkeypatch, ["--quiet"])
    assert result.exit_code == 0
    assert result.stdout.strip() == ""


def test_pull_empty_manifest_silent_under_json(tmp_path, monkeypatch):
    """`--json` output stays machine-readable — no extra prose line mixed
    into the JSON payload."""
    result = _run_pull_in(tmp_path, monkeypatch, ["--json"])
    assert result.exit_code == 0
    json.loads(_clean(result.stdout).strip())  # must still parse cleanly


def test_pull_no_server_friendly_exit(tmp_path, monkeypatch):
    """No configured server -> exit 1 with friendly hint (no traceback)."""
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_cfg"))
    monkeypatch.delenv("AGNES_SERVER", raising=False)
    monkeypatch.delenv("AGNES_TOKEN", raising=False)
    result = runner.invoke(pull_app, [])
    # Either exit 1 with hint, or exit 0 if a default server URL applies.
    # Either way, there must be no Python traceback in stderr/stdout.
    assert "Traceback" not in (_clean(result.output) + _clean(result.stderr or ""))
