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
def _ensure_no_sentinel_leak(monkeypatch, request):
    """Pytest test order is not guaranteed; explicitly clear the recursion
    sentinel before every test so a leaked value from a prior test doesn't
    produce a false-positive 'cleared on exit' assertion.

    Also default ``_python_is_uv_tool_install`` to True so the bulk of
    existing tests (which exercise the uv install path) keep passing
    without each one having to mock the routing helper itself. Tests that
    exercise the pip-fallback path flip it back to False explicitly. Unit
    tests that exercise the helper *itself* opt out via
    ``@pytest.mark.no_routing_override``."""
    monkeypatch.delenv("AGNES_SELF_UPGRADE_IN_PROGRESS", raising=False)
    if "no_routing_override" not in request.keywords:
        monkeypatch.setattr(
            "cli.commands.self_upgrade._python_is_uv_tool_install",
            lambda: True,
        )
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


def test_pip_fallback_uses_sys_executable_not_user(monkeypatch):
    """pip path must target the running interpreter's venv, never --user."""
    monkeypatch.setattr(
        "cli.commands.self_upgrade._python_is_uv_tool_install", lambda: False,
    )
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


def test_uv_available_but_python_outside_uv_tools_uses_pip(monkeypatch):
    """uv is on PATH but agnes runs from a project venv (sys.executable
    is NOT under uv's tool-install root). Self-upgrade MUST route through
    pip targeting sys.executable so the active binary is upgraded.

    Before this fix the routing condition was just `shutil.which("uv")`,
    which led to `uv tool install --force` rewriting `~/.local/bin/agnes`
    (a different binary entirely) while the user's `.venv/bin/agnes`
    stayed stale forever — and the stale-version banner spammed every
    subsequent command output because self-upgrade reported success but
    the running binary never changed."""
    monkeypatch.setattr(
        "cli.commands.self_upgrade._python_is_uv_tool_install", lambda: False,
    )
    # uv IS on PATH (otherwise the bug doesn't manifest — pip path would
    # be taken anyway). The fix's job is to route on whether the running
    # python belongs to uv-tool, not on whether uv exists.
    with patch("cli.commands.self_upgrade.check", return_value=_outdated_info()), \
         patch("cli.commands.self_upgrade.shutil.which",
               side_effect=lambda name: "/usr/local/bin/uv" if name == "uv" else None), \
         patch("cli.commands.self_upgrade.subprocess.run") as mock_run, \
         patch("cli.commands.self_upgrade._smoke_test_new_binary", return_value=_smoke_pass()), \
         patch("cli.commands.self_upgrade._read_last_known_good", return_value=None), \
         patch("cli.commands.self_upgrade._record_last_known_good"), \
         patch("cli.commands.self_upgrade._invalidate_update_cache"):
        mock_run.return_value = MagicMock(returncode=0)
        result = runner.invoke(app, ["self-upgrade"])
        assert result.exit_code == 0
        cmds = [c.args[0] for c in mock_run.call_args_list]
        assert not any(
            len(cmd) >= 3 and cmd[:3] == ["uv", "tool", "install"] for cmd in cmds
        ), f"uv tool install was called despite python being outside uv-tool root: {cmds}"
        pip_cmd = next(cmd for cmd in cmds if "pip" in cmd)
        assert pip_cmd[0] == sys.executable


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
    """Convention in `_do_install_with_smoke_and_rollback`: record, then
    invalidate. The OTHER invalidate call here (the FIRST one in call_order)
    is the pre-probe invalidate inside `_resolve_info` that ensures
    `agnes self-upgrade` always re-probes /cli/latest instead of trusting
    the 24h cache — see `test_self_upgrade_bypasses_24h_cache_without_force`.
    Both invalidates are intentional; we pin only the record→invalidate pair
    of the post-install bookkeeping by looking at the LAST invalidate."""
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
        # LAST invalidate — the post-install bookkeeping one.
        invalidate_idx = max(
            i for i, c in enumerate(call_order) if c[0] == "invalidate"
        )
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


def test_self_upgrade_bypasses_24h_cache_without_force(tmp_path, monkeypatch):
    """Plain `agnes self-upgrade` (no --force) MUST re-probe /cli/latest
    even when the local update_check.json cache claims we're current.

    Pre-fix the cache short-circuited and the command was a silent no-op
    after a server bump within the 24h window. Empirically observed:
    prod 0.47.1 → 0.47.2 didn't propagate to clients with a fresh cache.
    """
    import json
    import time
    from cli.commands import self_upgrade as su
    from cli import update_check as uc

    # Redirect the on-disk cache to tmp_path via _config_dir's env override.
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path))

    # Arrange: stale cache claims installed=latest=0.47.1, written 1 minute
    # ago — well within the 24h positive-cache TTL.
    cache_path = tmp_path / "update_check.json"
    cache_path.write_text(json.dumps({
        "installed": "0.47.1",
        "server_url": "http://server.test",
        "latest": "0.47.1",
        "download_url": "http://server.test/cli/wheel/agnes-0.47.1-py3-none-any.whl",
        "checked_at": time.time() - 60,
    }), encoding="utf-8")

    # Mock the network probe to return 0.47.2 — the bumped server.
    monkeypatch.setattr(uc, "_fetch_latest", lambda url: {
        "version": "0.47.2",
        "download_url_path": "/cli/wheel/agnes-0.47.2-py3-none-any.whl",
    })
    # Pin the installed version to 0.47.1 (matches the stale cache).
    monkeypatch.setattr(uc, "_installed_version", lambda: "0.47.1")
    # Pin the server URL so the cache key matches.
    monkeypatch.setattr(su, "get_server_url", lambda: "http://server.test")

    # Act: explicit self-upgrade WITHOUT --force.
    info = su._resolve_info(force=False)

    # Assert: returns UpdateInfo carrying the FRESH 0.47.2, not cached 0.47.1.
    assert info is not None and not isinstance(info, su._Unreachable)
    assert info.latest == "0.47.2", (
        f"expected fresh probe to return 0.47.2; got {info.latest} "
        "(cache short-circuit regressed)"
    )
    assert info.installed == "0.47.1"
    assert info.download_url == (
        "http://server.test/cli/wheel/agnes-0.47.2-py3-none-any.whl"
    )

    # Assert: cache was rewritten with the fresh latest. Proves the probe
    # actually ran rather than the stale cache satisfying the call via
    # some other path that happened to leave 0.47.1 untouched on disk.
    refreshed = json.loads(cache_path.read_text(encoding="utf-8"))
    assert refreshed["latest"] == "0.47.2"


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


# ---------------------------------------------------------------------------
# Workspace hook auto-refresh (PR #242 — ZdenekSrotyr #2 silent-stop fix)
# ---------------------------------------------------------------------------


def test_hook_refresh_fires_when_cli_already_current(monkeypatch):
    """The info-is-None fast path must still refresh hooks. Covers the
    v0.48→v0.49 migration moment when the operator already self-upgraded
    the CLI (so the second self-upgrade call from a SessionStart hook
    finds nothing to install), but their workspace settings.json was
    written by the older CLI version and lacks the new capture-session
    hook entry."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", "/fake/workspace")
    with patch("cli.commands.self_upgrade.check", return_value=_current_info()), \
         patch("cli.commands.self_upgrade.maybe_refresh_claude_hooks") as mock_refresh:
        result = runner.invoke(app, ["self-upgrade"])
        assert result.exit_code == 0
        mock_refresh.assert_called_once()


def test_hook_refresh_fires_after_successful_install(monkeypatch):
    """The install-success path must refresh hooks AFTER the new wheel is
    in place — so any wire-format change in the new release lands on the
    next session-start without re-running `agnes init`."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", "/fake/workspace")
    with patch("cli.commands.self_upgrade.check", return_value=_outdated_info()), \
         patch("cli.commands.self_upgrade.shutil.which", return_value="/usr/local/bin/uv"), \
         patch("cli.commands.self_upgrade.subprocess.run") as mock_run, \
         patch("cli.commands.self_upgrade._smoke_test_new_binary", return_value=_smoke_pass()), \
         patch("cli.commands.self_upgrade._read_last_known_good", return_value=None), \
         patch("cli.commands.self_upgrade._record_last_known_good"), \
         patch("cli.commands.self_upgrade._invalidate_update_cache"), \
         patch("cli.commands.self_upgrade.maybe_refresh_claude_hooks") as mock_refresh:
        mock_run.return_value = MagicMock(returncode=0)
        result = runner.invoke(app, ["self-upgrade"])
        assert result.exit_code == 0
        mock_refresh.assert_called_once()


def test_hook_refresh_skipped_on_install_failure(monkeypatch):
    """Failed install: do NOT refresh hooks — the rollback has already
    run and the workspace is in a known-prior state; rewriting hooks now
    could pin a layout that doesn't match the rolled-back binary."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", "/fake/workspace")
    with patch("cli.commands.self_upgrade.check", return_value=_outdated_info()), \
         patch("cli.commands.self_upgrade.shutil.which", return_value="/usr/local/bin/uv"), \
         patch("cli.commands.self_upgrade.subprocess.run") as mock_run, \
         patch("cli.commands.self_upgrade._smoke_test_new_binary", return_value=_smoke_fail()), \
         patch("cli.commands.self_upgrade._read_last_known_good", return_value=_PRIOR_URL), \
         patch("cli.commands.self_upgrade._record_last_known_good"), \
         patch("cli.commands.self_upgrade._invalidate_update_cache"), \
         patch("cli.commands.self_upgrade.maybe_refresh_claude_hooks") as mock_refresh:
        mock_run.return_value = MagicMock(returncode=0)  # install rc=0 but smoke failed
        result = runner.invoke(app, ["self-upgrade"])
        assert result.exit_code == 1
        mock_refresh.assert_not_called()


def test_hook_refresh_skipped_when_check_only(monkeypatch):
    """--check-only is read-only intent; never touch the workspace."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", "/fake/workspace")
    with patch("cli.commands.self_upgrade.check", return_value=_outdated_info()), \
         patch("cli.commands.self_upgrade.maybe_refresh_claude_hooks") as mock_refresh:
        result = runner.invoke(app, ["self-upgrade", "--check-only"])
        # exit 1 because outdated — see test_check_only_when_outdated_exits_1
        assert result.exit_code == 1
        mock_refresh.assert_not_called()


def test_hook_refresh_failure_does_not_flip_exit_code(monkeypatch):
    """An exception inside maybe_refresh_claude_hooks must NOT turn a
    successful upgrade into rc=1. The refresh is best-effort."""
    monkeypatch.setenv("AGNES_LOCAL_DIR", "/fake/workspace")
    with patch("cli.commands.self_upgrade.check", return_value=_current_info()), \
         patch("cli.commands.self_upgrade.maybe_refresh_claude_hooks",
               side_effect=PermissionError("settings.json read-only")):
        result = runner.invoke(app, ["self-upgrade"])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# _python_is_uv_tool_install — install-manager detection
# ---------------------------------------------------------------------------


@pytest.mark.no_routing_override
def test_python_is_uv_tool_install_no_uv_on_path(monkeypatch):
    """No uv on PATH → not a uv-tool install (routes to pip)."""
    from cli.commands import self_upgrade as su
    monkeypatch.setattr("cli.commands.self_upgrade.shutil.which", lambda _name: None)
    assert su._python_is_uv_tool_install() is False


@pytest.mark.no_routing_override
def test_python_is_uv_tool_install_sys_executable_under_uv_root(monkeypatch, tmp_path):
    """`sys.executable` lives under `uv tool dir` → uv-tool install."""
    from cli.commands import self_upgrade as su
    fake_uv_root = tmp_path / "uv" / "tools"
    fake_python = fake_uv_root / "agnes-the-ai-analyst" / "bin" / "python"
    fake_python.parent.mkdir(parents=True)
    fake_python.touch()
    monkeypatch.setattr("cli.commands.self_upgrade.shutil.which",
                        lambda name: "/usr/local/bin/uv" if name == "uv" else None)
    monkeypatch.setattr(
        "cli.commands.self_upgrade.subprocess.run",
        lambda *a, **kw: MagicMock(returncode=0, stdout=f"{fake_uv_root}\n"),
    )
    monkeypatch.setattr("cli.commands.self_upgrade.sys.executable", str(fake_python))
    assert su._python_is_uv_tool_install() is True


@pytest.mark.no_routing_override
def test_python_is_uv_tool_install_sys_executable_in_project_venv(monkeypatch, tmp_path):
    """`sys.executable` is in a project venv outside uv's tool root →
    not a uv-tool install (routes to pip). This is the bug scenario: uv
    is installed (perhaps the user has other uv-managed tools) but agnes
    came in via `pip install -e .` into a project venv."""
    from cli.commands import self_upgrade as su
    fake_uv_root = tmp_path / "uv" / "tools"
    fake_uv_root.mkdir(parents=True)
    project_venv_python = tmp_path / "project" / ".venv" / "bin" / "python"
    project_venv_python.parent.mkdir(parents=True)
    project_venv_python.touch()
    monkeypatch.setattr("cli.commands.self_upgrade.shutil.which",
                        lambda name: "/usr/local/bin/uv" if name == "uv" else None)
    monkeypatch.setattr(
        "cli.commands.self_upgrade.subprocess.run",
        lambda *a, **kw: MagicMock(returncode=0, stdout=f"{fake_uv_root}\n"),
    )
    monkeypatch.setattr(
        "cli.commands.self_upgrade.sys.executable", str(project_venv_python),
    )
    assert su._python_is_uv_tool_install() is False


@pytest.mark.no_routing_override
def test_python_is_uv_tool_install_uv_tool_dir_nonzero_exit(monkeypatch):
    """`uv tool dir` exits non-zero (e.g. corrupt uv config) → treat as
    not-uv-tool so self-upgrade falls back to pip rather than crashing."""
    from cli.commands import self_upgrade as su
    monkeypatch.setattr("cli.commands.self_upgrade.shutil.which",
                        lambda name: "/usr/local/bin/uv" if name == "uv" else None)
    monkeypatch.setattr(
        "cli.commands.self_upgrade.subprocess.run",
        lambda *a, **kw: MagicMock(returncode=1, stdout=""),
    )
    assert su._python_is_uv_tool_install() is False
