"""Tests for cli/upgrade_status.py — silent self-upgrade failure surfacing (#478).

Covers the persistence primitive (counter increments on failure, resets on
success), the `should_warn` threshold (>=3), and the banner emission via the
root callback warning ONCE on the next NON-quiet command while staying silent
under --quiet."""

import json

import pytest
from typer.testing import CliRunner

from cli import upgrade_status as us
from cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _tmp_config(tmp_path, monkeypatch):
    """Redirect `_config_dir()` to a tmp dir so the on-disk status file is
    isolated per test. Also silence the version probe so the only stderr
    line under test is the upgrade-failure warning."""
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AGNES_NO_UPDATE_CHECK", "1")
    monkeypatch.delenv("AGNES_SELF_UPGRADE_IN_PROGRESS", raising=False)
    yield tmp_path


def _status(tmp_path) -> dict:
    p = tmp_path / "upgrade_status.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


# --- persistence primitive --------------------------------------------------


def test_failure_increments_counter(_tmp_config):
    us.record_outcome(success=False)
    assert us.consecutive_failures() == 1
    us.record_outcome(success=False)
    assert us.consecutive_failures() == 2
    s = _status(_tmp_config)
    assert s["last_outcome"] == "failure"
    assert isinstance(s["last_attempt_ts"], (int, float))


def test_success_resets_counter(_tmp_config):
    us.record_outcome(success=False)
    us.record_outcome(success=False)
    us.record_outcome(success=False)
    assert us.consecutive_failures() == 3
    us.record_outcome(success=True)
    assert us.consecutive_failures() == 0
    assert _status(_tmp_config)["last_outcome"] == "success"


def test_should_warn_threshold_is_three(_tmp_config):
    for _ in range(2):
        us.record_outcome(success=False)
    assert us.should_warn() is False  # 2 failures — below threshold
    us.record_outcome(success=False)
    assert us.should_warn() is True  # 3 failures — at threshold


def test_read_status_tolerates_missing_and_malformed(_tmp_config):
    assert us.read_status() == {}
    (_tmp_config / "upgrade_status.json").write_text("not json {", encoding="utf-8")
    assert us.read_status() == {}
    assert us.consecutive_failures() == 0


def test_format_failure_notice_includes_count(_tmp_config):
    for _ in range(3):
        us.record_outcome(success=False)
    msg = us.format_failure_notice()
    assert "3 times" in msg
    assert "agnes self-upgrade" in msg


# --- banner emission via the root callback ----------------------------------


def test_banner_warns_on_next_non_quiet_command(_tmp_config, monkeypatch):
    """After >=3 silent failures, the next NON-quiet command warns once.

    `_command_is_quiet()` inspects ``sys.argv`` — exactly what the real CLI
    sees (``agnes catalog`` → ``sys.argv == ['agnes', 'catalog']``). The
    CliRunner does not populate ``sys.argv``, so we set it explicitly to
    model the real invocation. `agnes catalog --help` exercises the root
    callback (which fires before --help short-circuits the subcommand)."""
    for _ in range(3):
        us.record_outcome(success=False)
    monkeypatch.setattr("sys.argv", ["agnes", "catalog", "--help"])
    result = runner.invoke(app, ["catalog", "--help"])
    assert "self-upgrade has failed 3 times" in result.stderr


def test_banner_silent_under_quiet(_tmp_config, monkeypatch):
    """--quiet (the SessionStart hook path) must NOT emit the warning."""
    for _ in range(3):
        us.record_outcome(success=False)
    monkeypatch.setattr("sys.argv", ["agnes", "pull", "--quiet"])
    result = runner.invoke(app, ["pull", "--quiet"])
    assert "self-upgrade has failed" not in (result.stderr or "")


def test_banner_silent_below_threshold(_tmp_config, monkeypatch):
    """Two failures is below the N=3 threshold — no warning."""
    us.record_outcome(success=False)
    us.record_outcome(success=False)
    monkeypatch.setattr("sys.argv", ["agnes", "catalog", "--help"])
    result = runner.invoke(app, ["catalog", "--help"])
    assert "self-upgrade has failed" not in (result.stderr or "")


def test_banner_silent_during_self_upgrade_subprocess(_tmp_config, monkeypatch):
    """The smoke-test `agnes --version` runs with the recursion sentinel set;
    it must not emit the failure warning (would pollute the smoke output)."""
    for _ in range(3):
        us.record_outcome(success=False)
    monkeypatch.setattr("sys.argv", ["agnes", "--version"])
    monkeypatch.setenv("AGNES_SELF_UPGRADE_IN_PROGRESS", "1")
    result = runner.invoke(app, ["catalog", "--help"])
    assert "self-upgrade has failed" not in (result.stderr or "")


def test_banner_warns_once_then_stays_silent(_tmp_config, monkeypatch):
    """The warning fires on the NEXT non-quiet command, then stays silent on
    subsequent commands at the same failure level (no spam)."""
    for _ in range(3):
        us.record_outcome(success=False)
    monkeypatch.setattr("sys.argv", ["agnes", "catalog", "--help"])
    first = runner.invoke(app, ["catalog", "--help"])
    assert "self-upgrade has failed 3 times" in first.stderr
    # Second non-quiet command at the same level — no warning.
    second = runner.invoke(app, ["catalog", "--help"])
    assert "self-upgrade has failed" not in (second.stderr or "")


def test_banner_rearms_on_higher_failure_count(_tmp_config, monkeypatch):
    """A fresh failure (count climbs to 4) re-arms the warning so the
    analyst sees the situation is getting worse, not better."""
    for _ in range(3):
        us.record_outcome(success=False)
    monkeypatch.setattr("sys.argv", ["agnes", "catalog", "--help"])
    runner.invoke(app, ["catalog", "--help"])  # warns at 3, marks warned
    assert us.should_warn() is False  # silenced at 3
    us.record_outcome(success=False)  # now 4 failures
    assert us.should_warn() is True
    again = runner.invoke(app, ["catalog", "--help"])
    assert "self-upgrade has failed 4 times" in again.stderr
