"""Tests for `agnes self-upgrade` — install path, smoke test, rollback
(with rc capture), recursion barrier, --force offline failure, AGNES_NO_UPDATE_CHECK
bypass for explicit upgrades, --quiet stderr behavior, version-mismatch
smoke detection."""

import os
import sys
from unittest.mock import patch, MagicMock

import pytest
from typer.testing import CliRunner

from cli.main import app
from cli.update_check import UpdateInfo

runner = CliRunner()


@pytest.fixture(autouse=True)
def _ensure_no_sentinel_leak(monkeypatch):
    """Pytest test order is not guaranteed; explicitly clear the recursion
    sentinel before every test so a leaked value from a prior test doesn't
    produce a false-positive 'cleared on exit' assertion."""
    monkeypatch.delenv("AGNES_SELF_UPGRADE_IN_PROGRESS", raising=False)
    yield


_OUTDATED_URL = "http://server.test/cli/wheel/agnes-0.40.0-py3-none-any.whl"
_PRIOR_URL = "http://server.test/cli/wheel/agnes-0.35.0-py3-none-any.whl"


def _outdated_info():
    return UpdateInfo(installed="0.30.0", latest="0.40.0", download_url=_OUTDATED_URL)


def _current_info():
    return UpdateInfo(installed="0.40.0", latest="0.40.0", download_url=None)


def _smoke_pass():
    return (True, "agnes 0.40.0")


def _smoke_fail():
    return (False, "exit 1: ImportError: cannot import name 'foo'")


def test_check_only_when_outdated_exits_1():
    with patch("cli.commands.self_upgrade.check", return_value=_outdated_info()):
        result = runner.invoke(app, ["self-upgrade", "--check-only"])
        assert result.exit_code == 1
        assert "out of date" in result.output


def test_check_only_when_current_exits_0():
    with patch("cli.commands.self_upgrade.check", return_value=_current_info()):
        result = runner.invoke(app, ["self-upgrade", "--check-only"])
        assert result.exit_code == 0


def test_when_current_short_circuits_no_install():
    with patch("cli.commands.self_upgrade.check", return_value=_current_info()), \
         patch("cli.commands.self_upgrade.subprocess.run") as mock_run:
        result = runner.invoke(app, ["self-upgrade"])
        assert result.exit_code == 0
        mock_run.assert_not_called()


def test_uv_path_when_uv_available():
    with patch("cli.commands.self_upgrade.check", return_value=_outdated_info()), \
         patch("cli.commands.self_upgrade.shutil.which", return_value="/usr/local/bin/uv"), \
         patch("cli.commands.self_upgrade.subprocess.run") as mock_run, \
         patch("cli.commands.self_upgrade._smoke_test_new_binary", return_value=_smoke_pass()), \
         patch("cli.commands.self_upgrade._read_last_known_good", return_value=None), \
         patch("cli.commands.self_upgrade._record_last_known_good"), \
         patch("cli.commands.self_upgrade._invalidate_update_cache"):
        mock_run.return_value = MagicMock(returncode=0)
        result = runner.invoke(app, ["self-upgrade"])
        assert result.exit_code == 0
        args = mock_run.call_args_list[0].args[0]
        assert args[:3] == ["uv", "tool", "install"]
        assert "--force" in args
        assert _OUTDATED_URL in args


def test_pip_fallback_uses_sys_executable_not_user():
    """pip path must target the running interpreter's venv, never --user."""
    with patch("cli.commands.self_upgrade.check", return_value=_outdated_info()), \
         patch("cli.commands.self_upgrade.shutil.which", return_value=None), \
         patch("cli.commands.self_upgrade.subprocess.run") as mock_run, \
         patch("cli.commands.self_upgrade._smoke_test_new_binary", return_value=_smoke_pass()), \
         patch("cli.commands.self_upgrade._read_last_known_good", return_value=None), \
         patch("cli.commands.self_upgrade._record_last_known_good"), \
         patch("cli.commands.self_upgrade._invalidate_update_cache"):
        mock_run.return_value = MagicMock(returncode=0)
        result = runner.invoke(app, ["self-upgrade"])
        assert result.exit_code == 0
        cmds = [c.args[0] for c in mock_run.call_args_list]
        assert any(cmd[0] == "curl" for cmd in cmds), cmds
        pip_cmd = next(cmd for cmd in cmds if "pip" in cmd)
        assert pip_cmd[0] == sys.executable, pip_cmd
        assert "--force-reinstall" in pip_cmd
        assert "--user" not in pip_cmd


def test_force_invalidates_cache_before_check():
    """--force must drop the cached download_url before probing /cli/latest."""
    fresh_current_with_url = UpdateInfo(installed="0.40.0", latest="0.40.0",
                                        download_url=_OUTDATED_URL)
    with patch("cli.commands.self_upgrade._invalidate_update_cache") as mock_invalidate, \
         patch("cli.commands.self_upgrade.check", return_value=fresh_current_with_url) as mock_check, \
         patch("cli.commands.self_upgrade.shutil.which", return_value="/usr/local/bin/uv"), \
         patch("cli.commands.self_upgrade.subprocess.run") as mock_run, \
         patch("cli.commands.self_upgrade._smoke_test_new_binary", return_value=_smoke_pass()), \
         patch("cli.commands.self_upgrade._read_last_known_good", return_value=None), \
         patch("cli.commands.self_upgrade._record_last_known_good"):
        mock_run.return_value = MagicMock(returncode=0)
        result = runner.invoke(app, ["self-upgrade", "--force"])
        assert result.exit_code == 0
        assert mock_invalidate.call_count == 2
        mock_check.assert_called_once()


def test_force_offline_exits_1_with_stderr():
    """--force + server unreachable: exit 1 with explicit stderr."""
    with patch("cli.commands.self_upgrade.check", return_value=None), \
         patch("cli.commands.self_upgrade.get_server_url",
               return_value="http://server.test"), \
         patch("cli.commands.self_upgrade._invalidate_update_cache"):
        result = runner.invoke(app, ["self-upgrade", "--force"])
        assert result.exit_code == 1
        assert "cannot reach" in result.stderr
        assert "server.test" in result.stderr


def test_offline_without_force_is_silent():
    """No --force, server unreachable: exit 0 silently from self-upgrade
    itself. (The root callback's warning loop in cli/main.py may still emit
    `[update] …` to stderr — that's a separate code path; this test only
    pins that self-upgrade does not add a `cannot reach …` error.)"""
    with patch("cli.commands.self_upgrade.check", return_value=None), \
         patch("cli.commands.self_upgrade._invalidate_update_cache"):
        result = runner.invoke(app, ["self-upgrade"])
        assert result.exit_code == 0
        assert "cannot reach" not in result.stderr
        assert "self-upgrade:" not in result.stderr


def test_self_upgrade_passes_bypass_disabled_to_check():
    """AGNES_NO_UPDATE_CHECK silences the implicit warning loop, but
    explicit `agnes self-upgrade` must NOT be a silent no-op when set."""
    with patch("cli.commands.self_upgrade.check", return_value=_current_info()) as mock_check:
        result = runner.invoke(app, ["self-upgrade", "--check-only"])
        assert result.exit_code == 0
        kwargs = mock_check.call_args.kwargs
        assert kwargs.get("bypass_disabled") is True


def test_quiet_does_not_suppress_install_failure_stderr():
    """--quiet suppresses progress but install/smoke failures always surface."""
    with patch("cli.commands.self_upgrade.check", return_value=_outdated_info()), \
         patch("cli.commands.self_upgrade.shutil.which", return_value="/usr/local/bin/uv"), \
         patch("cli.commands.self_upgrade.subprocess.run") as mock_run, \
         patch("cli.commands.self_upgrade._read_last_known_good", return_value=None):
        mock_run.return_value = MagicMock(returncode=42)
        result = runner.invoke(app, ["self-upgrade", "--quiet"])
        assert result.exit_code == 1
        assert "install failed" in result.stderr


def test_smoke_fail_triggers_rollback_when_prior_url_known():
    """Broken new wheel: smoke fails, rollback to last-known-good URL, exit 1."""
    with patch("cli.commands.self_upgrade.check", return_value=_outdated_info()), \
         patch("cli.commands.self_upgrade.shutil.which", return_value="/usr/local/bin/uv"), \
         patch("cli.commands.self_upgrade.subprocess.run") as mock_run, \
         patch("cli.commands.self_upgrade._smoke_test_new_binary", return_value=_smoke_fail()), \
         patch("cli.commands.self_upgrade._read_last_known_good", return_value=_PRIOR_URL), \
         patch("cli.commands.self_upgrade._record_last_known_good") as mock_record:
        mock_run.return_value = MagicMock(returncode=0)
        result = runner.invoke(app, ["self-upgrade"])
        assert result.exit_code == 1
        urls_installed = [
            arg for c in mock_run.call_args_list
            for arg in c.args[0] if isinstance(arg, str) and arg.startswith("http")
        ]
        assert _OUTDATED_URL in urls_installed
        assert _PRIOR_URL in urls_installed
        mock_record.assert_not_called()
        assert "smoke test" in result.stderr


def test_smoke_fail_with_rollback_failure_surfaces_rc():
    """Forward install ok, smoke fail, rollback ALSO fails: stderr surfaces rc + recovery."""
    install_results = [MagicMock(returncode=0), MagicMock(returncode=99)]
    with patch("cli.commands.self_upgrade.check", return_value=_outdated_info()), \
         patch("cli.commands.self_upgrade.shutil.which", return_value="/usr/local/bin/uv"), \
         patch("cli.commands.self_upgrade.subprocess.run", side_effect=install_results), \
         patch("cli.commands.self_upgrade._smoke_test_new_binary", return_value=_smoke_fail()), \
         patch("cli.commands.self_upgrade._read_last_known_good", return_value=_PRIOR_URL), \
         patch("cli.commands.self_upgrade.get_server_url",
               return_value="http://server.test"):
        result = runner.invoke(app, ["self-upgrade"])
        assert result.exit_code == 1
        assert "rollback ALSO failed" in result.stderr
        assert "rc=99" in result.stderr
        assert "/cli/install.sh" in result.stderr


def test_smoke_fail_no_prior_url_prints_install_sh_recovery():
    """First-ever upgrade with no rollback target: stderr points at bootstrap path."""
    with patch("cli.commands.self_upgrade.check", return_value=_outdated_info()), \
         patch("cli.commands.self_upgrade.shutil.which", return_value="/usr/local/bin/uv"), \
         patch("cli.commands.self_upgrade.subprocess.run") as mock_run, \
         patch("cli.commands.self_upgrade._smoke_test_new_binary", return_value=_smoke_fail()), \
         patch("cli.commands.self_upgrade._read_last_known_good", return_value=None), \
         patch("cli.commands.self_upgrade.get_server_url",
               return_value="http://server.test"):
        mock_run.return_value = MagicMock(returncode=0)
        result = runner.invoke(app, ["self-upgrade"])
        assert result.exit_code == 1
        assert "/cli/install.sh" in result.stderr
        assert "server.test" in result.stderr


def test_smoke_pass_records_last_known_good_then_invalidates_cache():
    """Convention: record before invalidate."""
    call_order = []
    with patch("cli.commands.self_upgrade.check", return_value=_outdated_info()), \
         patch("cli.commands.self_upgrade.shutil.which", return_value="/usr/local/bin/uv"), \
         patch("cli.commands.self_upgrade.subprocess.run") as mock_run, \
         patch("cli.commands.self_upgrade._smoke_test_new_binary", return_value=_smoke_pass()), \
         patch("cli.commands.self_upgrade._read_last_known_good", return_value=None), \
         patch("cli.commands.self_upgrade._record_last_known_good",
               side_effect=lambda url: call_order.append(("record", url))), \
         patch("cli.commands.self_upgrade._invalidate_update_cache",
               side_effect=lambda: call_order.append(("invalidate", None))):
        mock_run.return_value = MagicMock(returncode=0)
        result = runner.invoke(app, ["self-upgrade"])
        assert result.exit_code == 0
        record_idx = next(i for i, c in enumerate(call_order) if c[0] == "record")
        invalidate_idx = next(i for i, c in enumerate(call_order) if c[0] == "invalidate")
        assert record_idx < invalidate_idx, call_order
        assert call_order[record_idx] == ("record", _OUTDATED_URL)


def test_self_upgrade_propagates_sentinel_to_smoke_subprocess():
    """The sentinel is set in os.environ during the run and cleared in finally."""
    captured_envs = []

    def _fake_smoke(method, expected_version):
        env = {**os.environ, "AGNES_NO_UPDATE_CHECK": "1",
               "AGNES_SELF_UPGRADE_IN_PROGRESS": "1"}
        captured_envs.append(env)
        return _smoke_pass()

    with patch("cli.commands.self_upgrade.check", return_value=_outdated_info()), \
         patch("cli.commands.self_upgrade.shutil.which", return_value="/usr/local/bin/uv"), \
         patch("cli.commands.self_upgrade.subprocess.run",
               return_value=MagicMock(returncode=0)), \
         patch("cli.commands.self_upgrade._smoke_test_new_binary", side_effect=_fake_smoke), \
         patch("cli.commands.self_upgrade._read_last_known_good", return_value=None), \
         patch("cli.commands.self_upgrade._record_last_known_good"), \
         patch("cli.commands.self_upgrade._invalidate_update_cache"):
        result = runner.invoke(app, ["self-upgrade"])
    assert result.exit_code == 0
    assert captured_envs and captured_envs[0]["AGNES_SELF_UPGRADE_IN_PROGRESS"] == "1"
    assert os.environ.get("AGNES_SELF_UPGRADE_IN_PROGRESS") is None


@pytest.mark.parametrize("install_method,patch_target", [
    ("uv", "_uv_tool_bin_path"),
    ("pip", "_pip_bin_path"),
])
def test_smoke_test_detects_version_mismatch(install_method, patch_target):
    """Smoke test execs binary at install path (NOT shutil.which) and checks
    Version equality (NOT substring). Parametrized over uv + pip."""
    from pathlib import Path
    from cli.commands import self_upgrade as su

    fake_bin = f"/fake/{install_method}/bin/agnes"
    with patch.object(su, patch_target, return_value=Path(fake_bin)), \
         patch.object(su.subprocess, "run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="agnes 0.30.0\n", stderr="")
        ok, detail = su._smoke_test_new_binary(install_method, expected_version="0.40.0")
        assert ok is False
        assert "version mismatch" in detail
        assert "0.40.0" in detail and "0.30.0" in detail
        assert mock_run.call_args.args[0][0] == fake_bin


def test_smoke_test_passes_with_pep440_local_version():
    """Use Version() comparison, not substring (so "0.40.0" doesn't match "0.40.10")."""
    from pathlib import Path
    from cli.commands import self_upgrade as su

    with patch.object(su, "_uv_tool_bin_path", return_value=Path("/fake/agnes")), \
         patch.object(su.subprocess, "run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="agnes 0.40.0\n", stderr="")
        ok, _ = su._smoke_test_new_binary("uv", expected_version="0.40.0")
        assert ok is True
        mock_run.return_value = MagicMock(returncode=0, stdout="agnes 0.40.10\n", stderr="")
        ok, detail = su._smoke_test_new_binary("uv", expected_version="0.40.0")
        assert ok is False
        assert "version mismatch" in detail
