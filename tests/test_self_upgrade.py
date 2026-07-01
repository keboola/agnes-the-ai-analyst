"""Tests for `agnes self-upgrade` — install path, smoke test, rollback
(with rc capture), recursion barrier, --force offline failure, AGNES_NO_UPDATE_CHECK
bypass for explicit upgrades, --quiet stderr behavior, version-mismatch
smoke detection."""

import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from typer.testing import CliRunner

from cli.main import app
from cli.update_check import UpdateInfo

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolate_config_dir(tmp_path, monkeypatch):
    """Point `_config_dir()` at a per-test tmp dir so self-upgrade's
    on-disk bookkeeping (last_known_good.json, update_check.json, and the
    upgrade_status.json failure counter added in #478) never touches the
    developer's real ~/.config/agnes and can't leak across tests."""
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_agnes_cfg"))
    yield


@pytest.fixture(autouse=True)
def _ensure_no_sentinel_leak(monkeypatch, request):
    """Pytest test order is not guaranteed; explicitly clear the recursion
    sentinel before every test so a leaked value from a prior test doesn't
    produce a false-positive 'cleared on exit' assertion.

    Also default ``_classify_install_method`` to the uv-tool path (and
    ``_python_is_uv_tool_install`` to True) so the bulk of existing tests
    (which exercise the uv install path) keep passing without each one having
    to mock routing. Tests that exercise the pip path override
    ``_classify_install_method`` to return ("venv", {}). Unit tests that
    exercise the routing helpers *themselves* opt out via
    ``@pytest.mark.no_routing_override``."""
    monkeypatch.delenv("AGNES_SELF_UPGRADE_IN_PROGRESS", raising=False)
    if "no_routing_override" not in request.keywords:
        monkeypatch.setattr(
            "cli.commands.self_upgrade._python_is_uv_tool_install",
            lambda: True,
        )
        monkeypatch.setattr(
            "cli.commands.self_upgrade._classify_install_method",
            lambda: ("uv-tool", {}),
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
        "cli.commands.self_upgrade._classify_install_method", lambda: ("venv", {}),
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
        "cli.commands.self_upgrade._classify_install_method", lambda: ("venv", {}),
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


def test_offline_without_force_does_not_touch_failure_counter():
    """Regression — Devin BUG_0001 on #601.

    Server-unreachable without `--force` (the implicit SessionStart hook
    path on a transient network blip) must NOT call `record_outcome(True)`
    — that would reset the consecutive-failure count an analyst has
    accumulated from real install failures, silently disarming the warning
    this feature exists to surface. Likewise it must NOT call
    `record_outcome(False)` — we have no opinion."""
    with patch("cli.commands.self_upgrade.check", return_value=None), \
         patch("cli.commands.self_upgrade._invalidate_update_cache"), \
         patch("cli.commands.self_upgrade.record_outcome") as mock_record:
        result = runner.invoke(app, ["self-upgrade"])
        assert result.exit_code == 0
        mock_record.assert_not_called()


def test_current_resets_failure_counter():
    """Companion to the offline test: when the server responds *and* the
    CLI is genuinely current, `record_outcome(True)` SHOULD fire — we have
    confirmation the CLI is in a known-good state. Locks the distinction
    the BUG_0001 fix introduces (_OFFLINE vs None)."""
    with patch("cli.commands.self_upgrade.check", return_value=_current_info()), \
         patch("cli.commands.self_upgrade._invalidate_update_cache"), \
         patch("cli.commands.self_upgrade.record_outcome") as mock_record, \
         patch("cli.commands.self_upgrade._try_refresh_hooks"):
        result = runner.invoke(app, ["self-upgrade"])
        assert result.exit_code == 0
        mock_record.assert_called_once_with(success=True)


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
         patch("cli.commands.self_upgrade._read_last_known_good_meta", return_value={}), \
         patch("cli.commands.self_upgrade._record_last_known_good_meta",
               side_effect=lambda meta: call_order.append(("record", meta.get("download_url")))), \
         patch("cli.commands.self_upgrade._record_wheel_cache", return_value={}), \
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

    def _fake_smoke(method, expected_version, *, user=False):
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
def test_python_is_uv_tool_install_sys_prefix_under_uv_root(monkeypatch, tmp_path):
    """`sys.prefix` (the venv dir) lives under `uv tool dir` → uv-tool install."""
    from cli.commands import self_upgrade as su
    fake_uv_root = tmp_path / "uv" / "tools"
    venv_dir = fake_uv_root / "agnes-the-ai-analyst"
    venv_dir.mkdir(parents=True)
    monkeypatch.setattr("cli.commands.self_upgrade.shutil.which",
                        lambda name: "/usr/local/bin/uv" if name == "uv" else None)
    monkeypatch.setattr(
        "cli.commands.self_upgrade.subprocess.run",
        lambda *a, **kw: MagicMock(returncode=0, stdout=f"{fake_uv_root}\n"),
    )
    monkeypatch.setattr("cli.commands.self_upgrade.sys.prefix", str(venv_dir))
    assert su._python_is_uv_tool_install() is True


@pytest.mark.no_routing_override
def test_python_is_uv_tool_install_symlinked_prefix(monkeypatch, tmp_path):
    """REGRESSION (#521): the venv's `bin/python` is a symlink to the base
    interpreter OUTSIDE the uv tree. Detection must anchor on `sys.prefix`
    (the real venv dir, under uv root) and return True. The old code did
    `Path(sys.executable).resolve()`, which followed the symlink out to the
    base interpreter → False → routed to pip → `No module named pip`.

    Both `sys.prefix` and `sys.executable` are set so that reverting to the
    `sys.executable`-based check re-breaks this test."""
    from cli.commands import self_upgrade as su
    fake_uv_root = tmp_path / "uv" / "tools"
    venv_dir = fake_uv_root / "agnes-the-ai-analyst"
    (venv_dir / "bin").mkdir(parents=True)
    outside_python = tmp_path / "homebrew" / "python3.12"
    outside_python.parent.mkdir(parents=True)
    outside_python.touch()
    venv_python = venv_dir / "bin" / "python"
    try:
        venv_python.symlink_to(outside_python)
    except (OSError, NotImplementedError):  # pragma: no cover — Windows w/o priv
        pytest.skip("symlink creation not permitted on this platform")
    monkeypatch.setattr("cli.commands.self_upgrade.shutil.which",
                        lambda name: "/usr/local/bin/uv" if name == "uv" else None)
    monkeypatch.setattr(
        "cli.commands.self_upgrade.subprocess.run",
        lambda *a, **kw: MagicMock(returncode=0, stdout=f"{fake_uv_root}\n"),
    )
    monkeypatch.setattr("cli.commands.self_upgrade.sys.prefix", str(venv_dir))
    monkeypatch.setattr("cli.commands.self_upgrade.sys.executable", str(venv_python))
    assert su._python_is_uv_tool_install() is True


@pytest.mark.no_routing_override
def test_python_is_uv_tool_install_sys_prefix_in_project_venv(monkeypatch, tmp_path):
    """`sys.prefix` is a project venv outside uv's tool root → not a uv-tool
    install (routes to pip). Bug scenario: uv is installed (other uv-managed
    tools) but agnes came in via `pip install -e .` into a project venv."""
    from cli.commands import self_upgrade as su
    fake_uv_root = tmp_path / "uv" / "tools"
    fake_uv_root.mkdir(parents=True)
    project_venv = tmp_path / "project" / ".venv"
    project_venv.mkdir(parents=True)
    monkeypatch.setattr("cli.commands.self_upgrade.shutil.which",
                        lambda name: "/usr/local/bin/uv" if name == "uv" else None)
    monkeypatch.setattr(
        "cli.commands.self_upgrade.subprocess.run",
        lambda *a, **kw: MagicMock(returncode=0, stdout=f"{fake_uv_root}\n"),
    )
    monkeypatch.setattr("cli.commands.self_upgrade.sys.prefix", str(project_venv))
    assert su._python_is_uv_tool_install() is False


@pytest.mark.no_routing_override
def test_path_is_within_containment_and_siblings(tmp_path):
    """`_path_is_within` is component-aware: a sibling with a shared prefix
    string (`/a/bc` vs `/a/b`) is NOT contained."""
    from cli.commands import self_upgrade as su
    parent = tmp_path / "a" / "b"
    assert su._path_is_within(parent / "c" / "d", parent) is True
    assert su._path_is_within(parent, parent) is True
    assert su._path_is_within(tmp_path / "a" / "bc", parent) is False


@pytest.mark.no_routing_override
def test_path_is_within_case_insensitive(monkeypatch):
    """On a case-insensitive filesystem (Windows/macOS), containment folds
    case. Simulate by monkeypatching normcase to lower-case."""
    from cli.commands import self_upgrade as su
    monkeypatch.setattr("cli.commands.self_upgrade.os.path.normcase", str.lower)
    monkeypatch.setattr("cli.commands.self_upgrade.os.path.realpath", lambda s: s)
    assert su._path_is_within(Path("/UV/Tools/Agnes"), Path("/uv/tools")) is True


@pytest.mark.no_routing_override
def test_path_is_within_cross_drive_is_false(monkeypatch):
    """Different Windows drives make `os.path.commonpath` raise ValueError;
    `_path_is_within` swallows it and returns False (routes to pip)."""
    from cli.commands import self_upgrade as su

    def _raise(_paths):
        raise ValueError("Paths don't have the same drive")

    monkeypatch.setattr("cli.commands.self_upgrade.os.path.commonpath", _raise)
    assert su._path_is_within(Path("/x"), Path("/y")) is False


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


# ---------------------------------------------------------------------------
# _classify_install_method — routing + refuse (FIX 2)
# ---------------------------------------------------------------------------


@pytest.mark.no_routing_override
def test_classify_editable_wins(monkeypatch):
    """An editable checkout classifies as "editable" even inside a venv."""
    from cli.commands import self_upgrade as su
    monkeypatch.setattr("cli.commands.self_upgrade._is_editable_install", lambda: True)
    assert su._classify_install_method() == ("editable", {})


@pytest.mark.no_routing_override
def test_classify_system_when_base_prefix(monkeypatch):
    """No venv / uv / pipx / user → system (base == prefix)."""
    from cli.commands import self_upgrade as su
    monkeypatch.setattr("cli.commands.self_upgrade._is_editable_install", lambda: False)
    monkeypatch.setattr("cli.commands.self_upgrade._python_is_uv_tool_install", lambda: False)
    monkeypatch.setattr("cli.commands.self_upgrade._in_pipx_venv", lambda: False)
    monkeypatch.setattr("cli.commands.self_upgrade._in_user_site", lambda: False)
    monkeypatch.setattr("cli.commands.self_upgrade.sys.prefix", "/usr")
    monkeypatch.setattr("cli.commands.self_upgrade.sys.base_prefix", "/usr")
    assert su._classify_install_method() == ("system", {})


@pytest.mark.no_routing_override
def test_classify_user_site(monkeypatch):
    """Not in a venv but package under the user site → ("user", {"user": True})."""
    from cli.commands import self_upgrade as su
    monkeypatch.setattr("cli.commands.self_upgrade._is_editable_install", lambda: False)
    monkeypatch.setattr("cli.commands.self_upgrade._python_is_uv_tool_install", lambda: False)
    monkeypatch.setattr("cli.commands.self_upgrade._in_pipx_venv", lambda: False)
    monkeypatch.setattr("cli.commands.self_upgrade.sys.prefix", "/usr")
    monkeypatch.setattr("cli.commands.self_upgrade.sys.base_prefix", "/usr")
    monkeypatch.setattr("cli.commands.self_upgrade._in_user_site", lambda: True)
    assert su._classify_install_method() == ("user", {"user": True})


def test_self_upgrade_refuses_editable(monkeypatch):
    """An editable install is refused: exit 1, no install subprocess runs."""
    with patch("cli.commands.self_upgrade.check", return_value=_outdated_info()), \
         patch("cli.commands.self_upgrade._classify_install_method", return_value=("editable", {})), \
         patch("cli.commands.self_upgrade.subprocess.run") as mock_run:
        result = runner.invoke(app, ["self-upgrade"])
        assert result.exit_code == 1
        mock_run.assert_not_called()  # nothing installed


def test_self_upgrade_refuses_system(monkeypatch):
    """A system/base Python is refused: exit 1, no install subprocess runs."""
    with patch("cli.commands.self_upgrade.check", return_value=_outdated_info()), \
         patch("cli.commands.self_upgrade._classify_install_method", return_value=("system", {})), \
         patch("cli.commands.self_upgrade.subprocess.run") as mock_run:
        result = runner.invoke(app, ["self-upgrade"])
        assert result.exit_code == 1
        mock_run.assert_not_called()


def test_user_method_passes_user_flag(monkeypatch):
    """method == user routes to pip with --user."""
    with patch("cli.commands.self_upgrade.check", return_value=_outdated_info()), \
         patch("cli.commands.self_upgrade._classify_install_method",
               return_value=("user", {"user": True})), \
         patch("cli.commands.self_upgrade.subprocess.run") as mock_run, \
         patch("cli.commands.self_upgrade._smoke_test_new_binary", return_value=_smoke_pass()), \
         patch("cli.commands.self_upgrade._read_last_known_good", return_value=None), \
         patch("cli.commands.self_upgrade._record_last_known_good"), \
         patch("cli.commands.self_upgrade._invalidate_update_cache"):
        mock_run.return_value = MagicMock(returncode=0)
        result = runner.invoke(app, ["self-upgrade"])
        assert result.exit_code == 0
        pip_cmd = next(c.args[0] for c in mock_run.call_args_list if "pip" in c.args[0])
        assert "--user" in pip_cmd


def test_pipx_method_routes_pip_no_user(monkeypatch):
    """method == pipx routes to pip WITHOUT --user (pipx venvs have pip)."""
    with patch("cli.commands.self_upgrade.check", return_value=_outdated_info()), \
         patch("cli.commands.self_upgrade._classify_install_method", return_value=("pipx", {})), \
         patch("cli.commands.self_upgrade.subprocess.run") as mock_run, \
         patch("cli.commands.self_upgrade._smoke_test_new_binary", return_value=_smoke_pass()), \
         patch("cli.commands.self_upgrade._read_last_known_good", return_value=None), \
         patch("cli.commands.self_upgrade._record_last_known_good"), \
         patch("cli.commands.self_upgrade._invalidate_update_cache"):
        mock_run.return_value = MagicMock(returncode=0)
        result = runner.invoke(app, ["self-upgrade"])
        assert result.exit_code == 0
        cmds = [c.args[0] for c in mock_run.call_args_list]
        assert not any(cmd[:3] == ["uv", "tool", "install"] for cmd in cmds if len(cmd) >= 3)
        pip_cmd = next(cmd for cmd in cmds if "pip" in cmd)
        assert "--user" not in pip_cmd


def test_install_with_pip_drops_no_deps(monkeypatch):
    """The pip install must NOT pass --no-deps (so a dep bump resolves)."""
    from cli.commands import self_upgrade as su
    calls = []
    monkeypatch.setattr(
        "cli.commands.self_upgrade.subprocess.run",
        lambda cmd, **kw: calls.append(cmd) or MagicMock(returncode=0),
    )
    assert su._install_with_pip("http://s/agnes.whl", quiet=True) == 0
    pip_cmd = next(c for c in calls if "pip" in c)
    assert "--no-deps" not in pip_cmd
    assert "--force-reinstall" in pip_cmd
    assert "--user" not in pip_cmd


def test_install_with_pip_user_adds_user_flag(monkeypatch):
    from cli.commands import self_upgrade as su
    calls = []
    monkeypatch.setattr(
        "cli.commands.self_upgrade.subprocess.run",
        lambda cmd, **kw: calls.append(cmd) or MagicMock(returncode=0),
    )
    assert su._install_with_pip("http://s/agnes.whl", quiet=True, user=True) == 0
    pip_cmd = next(c for c in calls if "pip" in c)
    assert "--user" in pip_cmd


# ---------------------------------------------------------------------------
# Local wheel cache / rollback-from-cache / preflight (FIX 3)
# ---------------------------------------------------------------------------


def test_record_and_cached_wheel_roundtrip(tmp_path):
    from cli.commands import self_upgrade as su
    src = tmp_path / "src.whl"
    src.write_bytes(b"wheelbytes")
    meta = su._record_wheel_cache("1.0.0", src)
    assert meta["version"] == "1.0.0"
    assert meta["wheel_filename"] == "1.0.0.whl"
    assert meta["sha256"]
    cached = su._cached_wheel_for(meta)
    assert cached is not None and cached.exists()


def test_cached_wheel_for_sha_mismatch_and_missing(tmp_path):
    from cli.commands import self_upgrade as su
    src = tmp_path / "src.whl"
    src.write_bytes(b"good")
    meta = su._record_wheel_cache("2.0.0", src)
    # tamper with the cached copy → sha no longer matches → None
    (su._wheel_cache_dir() / meta["wheel_filename"]).write_bytes(b"tampered")
    assert su._cached_wheel_for(meta) is None
    # missing file / empty meta → None
    assert su._cached_wheel_for({"wheel_filename": "nope.whl", "sha256": "x"}) is None
    assert su._cached_wheel_for({}) is None


def test_wheel_cache_gc_keeps_last_two(tmp_path):
    from cli.commands import self_upgrade as su
    for v in ("1.0.0", "1.1.0", "1.2.0"):
        src = tmp_path / f"{v}.whl"
        src.write_bytes(v.encode())
        su._record_wheel_cache(v, src)
    remaining = {p.name for p in su._wheel_cache_dir().glob("*.whl")}
    assert len(remaining) == 2, remaining
    assert "1.2.0.whl" in remaining  # the just-cached one is always kept


def test_read_last_known_good_backcompat(tmp_path):
    from cli.commands import self_upgrade as su
    su._record_last_known_good("http://s/agnes-1.0.0-py3-none-any.whl")
    assert su._read_last_known_good() == "http://s/agnes-1.0.0-py3-none-any.whl"
    assert su._read_last_known_good_meta()["download_url"].endswith(".whl")


def test_successful_install_caches_wheel_and_records_sha(tmp_path):
    """On smoke pass, the wheel is cached and last_known_good.json gets sha256."""
    from cli.commands import self_upgrade as su

    def fake_download(url, dest_dir, *, quiet):
        p = Path(dest_dir) / "agnes.whl"
        p.write_bytes(b"realwheel")
        return p

    with patch("cli.commands.self_upgrade.check", return_value=_outdated_info()), \
         patch("cli.commands.self_upgrade.shutil.which", return_value="/usr/local/bin/uv"), \
         patch("cli.commands.self_upgrade.subprocess.run", return_value=MagicMock(returncode=0)), \
         patch("cli.commands.self_upgrade._smoke_test_new_binary", return_value=_smoke_pass()), \
         patch("cli.commands.self_upgrade._read_last_known_good_meta", return_value={}), \
         patch("cli.commands.self_upgrade._download_wheel", side_effect=fake_download), \
         patch("cli.commands.self_upgrade._invalidate_update_cache"):
        result = runner.invoke(app, ["self-upgrade"])
        assert result.exit_code == 0
    meta = su._read_last_known_good_meta()
    assert meta.get("wheel_filename") and meta.get("sha256")
    assert su._cached_wheel_for(meta) is not None


def test_rollback_uses_cached_wheel_not_url(tmp_path):
    """Smoke fail with a valid cached prior wheel → rollback installs the LOCAL
    cached wheel, never the (possibly-404) prior URL."""
    from cli.commands import self_upgrade as su
    src = tmp_path / "prior.whl"
    src.write_bytes(b"priorwheel")
    prior_meta = {"download_url": _PRIOR_URL, **su._record_wheel_cache("0.35.0", src)}
    cached = su._cached_wheel_for(prior_meta)
    assert cached is not None

    installed = []
    with patch("cli.commands.self_upgrade.check", return_value=_outdated_info()), \
         patch("cli.commands.self_upgrade.shutil.which", return_value="/usr/local/bin/uv"), \
         patch("cli.commands.self_upgrade.subprocess.run",
               side_effect=lambda cmd, **kw: installed.append(cmd) or MagicMock(returncode=0)), \
         patch("cli.commands.self_upgrade._smoke_test_new_binary", return_value=_smoke_fail()), \
         patch("cli.commands.self_upgrade._read_last_known_good_meta", return_value=prior_meta):
        result = runner.invoke(app, ["self-upgrade"])
        assert result.exit_code == 1
        flat = [a for cmd in installed for a in cmd]
        assert str(cached) in flat            # rolled back from the local cache
        assert _PRIOR_URL not in flat          # never touched the prior URL


def test_rollback_skipped_when_no_cache_and_no_url(tmp_path):
    """Smoke fail, prior meta names a wheel whose cache is corrupt AND no URL →
    rollback skipped, bootstrap recovery printed."""
    from cli.commands import self_upgrade as su
    src = tmp_path / "prior.whl"
    src.write_bytes(b"priorwheel")
    meta = su._record_wheel_cache("0.35.0", src)  # no download_url key
    (su._wheel_cache_dir() / meta["wheel_filename"]).write_bytes(b"tampered")
    with patch("cli.commands.self_upgrade.check", return_value=_outdated_info()), \
         patch("cli.commands.self_upgrade.shutil.which", return_value="/usr/local/bin/uv"), \
         patch("cli.commands.self_upgrade.subprocess.run", return_value=MagicMock(returncode=0)), \
         patch("cli.commands.self_upgrade._smoke_test_new_binary", return_value=_smoke_fail()), \
         patch("cli.commands.self_upgrade._read_last_known_good_meta", return_value=meta), \
         patch("cli.commands.self_upgrade.get_server_url", return_value="http://server.test"):
        result = runner.invoke(app, ["self-upgrade"])
        assert result.exit_code == 1
        assert "no usable cached wheel" in result.stderr
        assert "/cli/install.sh" in result.stderr


def test_preflight_defers_unattended_when_artifact_missing(tmp_path):
    """--quiet + prior meta names a wheel that's missing → defer (exit 0), no
    install attempted, failure counter untouched."""
    from cli.commands import self_upgrade as su
    from cli import upgrade_status as us
    us.record_outcome(False)
    us.record_outcome(False)  # counter = 2
    prior_meta = {"download_url": _PRIOR_URL, "version": "0.35.0",
                  "wheel_filename": "0.35.0.whl", "sha256": "deadbeef"}  # file absent
    with patch("cli.commands.self_upgrade.check", return_value=_outdated_info()), \
         patch("cli.commands.self_upgrade.shutil.which", return_value="/usr/local/bin/uv"), \
         patch("cli.commands.self_upgrade.subprocess.run") as mock_run, \
         patch("cli.commands.self_upgrade._smoke_test_new_binary", return_value=_smoke_pass()), \
         patch("cli.commands.self_upgrade._read_last_known_good_meta", return_value=prior_meta):
        result = runner.invoke(app, ["self-upgrade", "--quiet"])
        assert result.exit_code == 0
        assert mock_run.call_count == 0            # nothing installed
    assert us.consecutive_failures() == 2          # counter untouched by a defer


def test_preflight_proceeds_first_ever_unattended(tmp_path):
    """--quiet + NO prior meta (first-ever) → proceed; blocking would freeze
    fresh installs."""
    with patch("cli.commands.self_upgrade.check", return_value=_outdated_info()), \
         patch("cli.commands.self_upgrade.shutil.which", return_value="/usr/local/bin/uv"), \
         patch("cli.commands.self_upgrade.subprocess.run") as mock_run, \
         patch("cli.commands.self_upgrade._smoke_test_new_binary", return_value=_smoke_pass()), \
         patch("cli.commands.self_upgrade._read_last_known_good_meta", return_value={}), \
         patch("cli.commands.self_upgrade._download_wheel", return_value=None), \
         patch("cli.commands.self_upgrade._invalidate_update_cache"):
        mock_run.return_value = MagicMock(returncode=0)
        result = runner.invoke(app, ["self-upgrade", "--quiet"])
        assert result.exit_code == 0
        cmds = [c.args[0] for c in mock_run.call_args_list]
        assert any(len(cmd) >= 3 and cmd[:3] == ["uv", "tool", "install"] for cmd in cmds)


# ---------------------------------------------------------------------------
# upgrade_status recording — silent self-upgrade failure surfacing (#478)
# ---------------------------------------------------------------------------


def test_install_failure_increments_failure_counter(monkeypatch, tmp_path):
    """A failed (quiet) self-upgrade increments the persisted failure
    counter so repeated silent failures become visible later."""
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path))
    from cli import upgrade_status as us
    with patch("cli.commands.self_upgrade.check", return_value=_outdated_info()), \
         patch("cli.commands.self_upgrade.shutil.which", return_value="/usr/local/bin/uv"), \
         patch("cli.commands.self_upgrade.subprocess.run") as mock_run, \
         patch("cli.commands.self_upgrade._read_last_known_good", return_value=None):
        mock_run.return_value = MagicMock(returncode=42)  # install fails
        result = runner.invoke(app, ["self-upgrade", "--quiet"])
        assert result.exit_code == 1
    assert us.consecutive_failures() == 1
    assert us.read_status()["last_outcome"] == "failure"


def test_successful_install_resets_failure_counter(monkeypatch, tmp_path):
    """A successful self-upgrade resets the counter to 0 even after prior
    failures."""
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path))
    from cli import upgrade_status as us
    us.record_outcome(success=False)
    us.record_outcome(success=False)
    assert us.consecutive_failures() == 2
    with patch("cli.commands.self_upgrade.check", return_value=_outdated_info()), \
         patch("cli.commands.self_upgrade.shutil.which", return_value="/usr/local/bin/uv"), \
         patch("cli.commands.self_upgrade.subprocess.run") as mock_run, \
         patch("cli.commands.self_upgrade._smoke_test_new_binary", return_value=_smoke_pass()), \
         patch("cli.commands.self_upgrade._read_last_known_good", return_value=None), \
         patch("cli.commands.self_upgrade._record_last_known_good"), \
         patch("cli.commands.self_upgrade._invalidate_update_cache"), \
         patch("cli.commands.self_upgrade.maybe_refresh_claude_hooks"):
        mock_run.return_value = MagicMock(returncode=0)
        result = runner.invoke(app, ["self-upgrade"])
        assert result.exit_code == 0
    assert us.consecutive_failures() == 0
    assert us.read_status()["last_outcome"] == "success"


def test_current_cli_resets_failure_counter(monkeypatch, tmp_path):
    """`agnes self-upgrade` when already current (info is None) records a
    success — the CLI is in a known-good state, so prior failures clear."""
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path))
    from cli import upgrade_status as us
    us.record_outcome(success=False)
    us.record_outcome(success=False)
    us.record_outcome(success=False)
    assert us.should_warn() is True
    with patch("cli.commands.self_upgrade.check", return_value=_current_info()), \
         patch("cli.commands.self_upgrade.maybe_refresh_claude_hooks"):
        result = runner.invoke(app, ["self-upgrade"])
        assert result.exit_code == 0
    assert us.consecutive_failures() == 0
    assert us.should_warn() is False


def test_smoke_fail_rollback_records_failure(monkeypatch, tmp_path):
    """A smoke-test rollback (forward install ok, new binary broken) counts
    as a failure for the counter."""
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path))
    from cli import upgrade_status as us
    with patch("cli.commands.self_upgrade.check", return_value=_outdated_info()), \
         patch("cli.commands.self_upgrade.shutil.which", return_value="/usr/local/bin/uv"), \
         patch("cli.commands.self_upgrade.subprocess.run") as mock_run, \
         patch("cli.commands.self_upgrade._smoke_test_new_binary", return_value=_smoke_fail()), \
         patch("cli.commands.self_upgrade._read_last_known_good", return_value=_PRIOR_URL), \
         patch("cli.commands.self_upgrade._record_last_known_good"):
        mock_run.return_value = MagicMock(returncode=0)
        result = runner.invoke(app, ["self-upgrade", "--quiet"])
        assert result.exit_code == 1
    assert us.consecutive_failures() == 1


def test_check_only_does_not_touch_failure_counter(monkeypatch, tmp_path):
    """`--check-only` is read-only intent — it must not record an outcome."""
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path))
    from cli import upgrade_status as us
    with patch("cli.commands.self_upgrade.check", return_value=_outdated_info()):
        result = runner.invoke(app, ["self-upgrade", "--check-only"])
        assert result.exit_code == 1
    assert us.read_status() == {}  # nothing recorded


# ---------------------------------------------------------------------------
# workspace_root back-fill (clients installed before the config anchor)
# ---------------------------------------------------------------------------


def test_backfill_sets_workspace_root_when_init_complete_present(tmp_path, monkeypatch):
    """A workspace with `.claude/init-complete` but no `workspace_root` in
    config gets the anchor recorded on the next self-upgrade SessionStart."""
    from cli.commands.self_upgrade import _maybe_backfill_workspace_root
    from cli.config import get_workspace_root

    ws = tmp_path / "ws"
    (ws / ".claude").mkdir(parents=True)
    (ws / ".claude" / "init-complete").write_text("completed_at: x\n", encoding="utf-8")
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(ws))

    assert get_workspace_root() is None
    _maybe_backfill_workspace_root()
    assert get_workspace_root() == str(ws.resolve())


def test_backfill_noop_without_init_complete(tmp_path, monkeypatch):
    """Never record a workspace_root for a dir that isn't an initialized
    workspace root — the `.claude/init-complete` sentinel is the guard that
    keeps a nested subfolder from being recorded."""
    from cli.commands.self_upgrade import _maybe_backfill_workspace_root
    from cli.config import get_workspace_root

    ws = tmp_path / "not-a-workspace"
    ws.mkdir()
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(ws))

    _maybe_backfill_workspace_root()
    assert get_workspace_root() is None


def test_backfill_does_not_overwrite_existing_workspace_root(tmp_path, monkeypatch):
    """Once set, the anchor is left alone — back-fill only fills a gap."""
    from cli.commands.self_upgrade import _maybe_backfill_workspace_root
    from cli.config import get_workspace_root, set_workspace_root

    set_workspace_root("/already/set")
    ws = tmp_path / "ws"
    (ws / ".claude").mkdir(parents=True)
    (ws / ".claude" / "init-complete").write_text("x\n", encoding="utf-8")
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(ws))

    _maybe_backfill_workspace_root()
    assert get_workspace_root() == "/already/set"


# ---------------------------------------------------------------------------
# Failure-reason persistence + surfacing (FIX 4)
# ---------------------------------------------------------------------------


def test_record_outcome_persists_and_clears_reason(tmp_path, monkeypatch):
    """A failure with a reason persists last_failure_reason; a later success
    clears it (and resets the counter)."""
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path))
    from cli import upgrade_status as us

    us.record_outcome(False, reason="smoke: exit 1: boom")
    assert us.read_status()["last_failure_reason"] == "smoke: exit 1: boom"
    us.record_outcome(True)
    assert "last_failure_reason" not in us.read_status()
    assert us.consecutive_failures() == 0


def test_record_outcome_redacts_secret_in_reason(tmp_path, monkeypatch):
    """A token embedded in the reason is scrubbed before it hits
    upgrade_status.json (which is not 0600)."""
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path))
    from cli import upgrade_status as us

    secret_url = "smoke: exit 1: 404 for https://s/cli/wheel/x.whl?token=SEKRET12345&sig=abcdef"
    us.record_outcome(False, reason=secret_url)
    stored = us.read_status()["last_failure_reason"]
    assert "SEKRET12345" not in stored
    assert "[REDACTED]" in stored
    assert len(stored) <= us._MAX_REASON_LEN


def test_format_failure_notice_appends_reason(tmp_path, monkeypatch):
    """The surfaced notice includes the recorded reason when present."""
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path))
    from cli import upgrade_status as us

    for _ in range(3):
        us.record_outcome(False, reason="smoke: version mismatch")
    notice = us.format_failure_notice()
    assert "failed 3 times" in notice
    assert "Last error: smoke: version mismatch" in notice


def test_smoke_fail_records_reason_end_to_end(tmp_path, monkeypatch):
    """A real smoke-fail self-upgrade persists a 'smoke:' reason."""
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path))
    from cli import upgrade_status as us
    with patch("cli.commands.self_upgrade.check", return_value=_outdated_info()), \
         patch("cli.commands.self_upgrade.shutil.which", return_value="/usr/local/bin/uv"), \
         patch("cli.commands.self_upgrade.subprocess.run", return_value=MagicMock(returncode=0)), \
         patch("cli.commands.self_upgrade._smoke_test_new_binary", return_value=_smoke_fail()), \
         patch("cli.commands.self_upgrade._read_last_known_good_meta", return_value={}):
        result = runner.invoke(app, ["self-upgrade", "--quiet"])
        assert result.exit_code == 1
    reason = us.read_status().get("last_failure_reason", "")
    assert reason.startswith("smoke:")


# ---------------------------------------------------------------------------
# Windows deferred self-update (detached helper) — branch selection + spawn
# ---------------------------------------------------------------------------


def test_windows_uv_tool_routes_to_deferred_helper(monkeypatch):
    """On Windows + uv-tool, self-upgrade STAGES a detached helper (exit 0) and
    does NOT attempt the in-place install that would self-lock and corrupt."""
    monkeypatch.setattr("cli.commands.self_upgrade.sys.platform", "win32")
    spawned = {"n": 0}

    def fake_spawn(info, prior_meta, *, quiet):
        spawned["n"] += 1
        return True

    monkeypatch.setattr("cli.commands.self_upgrade._spawn_windows_deferred_update", fake_spawn)
    with patch("cli.commands.self_upgrade.check", return_value=_outdated_info()), \
         patch("cli.commands.self_upgrade.shutil.which", return_value="uv.exe"), \
         patch("cli.commands.self_upgrade.subprocess.run") as mock_run, \
         patch("cli.commands.self_upgrade._read_last_known_good_meta", return_value={}):
        result = runner.invoke(app, ["self-upgrade", "--force"])
        assert result.exit_code == 0
        assert spawned["n"] == 1
        cmds = [c.args[0] for c in mock_run.call_args_list if c.args]
        assert not any(len(c) >= 3 and c[:3] == ["uv", "tool", "install"] for c in cmds), cmds


def test_windows_deferred_staging_failure_is_failsafe(monkeypatch):
    """If the helper can't be staged, self-upgrade FAILS SAFE (exit 1) — it must
    NOT fall through to the corrupting in-place swap."""
    monkeypatch.setattr("cli.commands.self_upgrade.sys.platform", "win32")
    monkeypatch.setattr("cli.commands.self_upgrade._spawn_windows_deferred_update",
                        lambda info, prior_meta, *, quiet: False)
    with patch("cli.commands.self_upgrade.check", return_value=_outdated_info()), \
         patch("cli.commands.self_upgrade.shutil.which", return_value="uv.exe"), \
         patch("cli.commands.self_upgrade.subprocess.run") as mock_run, \
         patch("cli.commands.self_upgrade._read_last_known_good_meta", return_value={}):
        result = runner.invoke(app, ["self-upgrade", "--force"])
        assert result.exit_code == 1
        cmds = [c.args[0] for c in mock_run.call_args_list if c.args]
        assert not any(len(c) >= 3 and c[:3] == ["uv", "tool", "install"] for c in cmds), cmds


@pytest.mark.no_routing_override
def test_spawn_windows_deferred_update_builds_detached_popen(monkeypatch, tmp_path):
    from cli.commands import self_upgrade as su
    monkeypatch.setattr("cli.commands.self_upgrade.sys.platform", "win32")
    monkeypatch.setattr("cli.commands.self_upgrade.shutil.which", lambda n: "uv.exe")
    monkeypatch.setattr(su, "_helper_interpreter", lambda: r"C:\base\python.exe")
    monkeypatch.setattr(su, "_wheel_cache_dir", lambda: tmp_path / "wheels")
    monkeypatch.setattr(su, "_config_dir", lambda: tmp_path / "cfg")
    monkeypatch.setattr(su, "_cached_wheel_for", lambda meta: None)

    def fake_dl(url, dest, *, quiet):
        p = Path(dest) / "agnes.whl"
        p.write_bytes(b"wheel")
        return p

    monkeypatch.setattr(su, "_download_wheel", fake_dl)
    captured = {}

    def fake_popen(argv, **kw):
        captured["argv"] = argv
        captured["kw"] = kw
        return object()

    monkeypatch.setattr("cli.commands.self_upgrade.subprocess.Popen", fake_popen)
    info = UpdateInfo(installed="0.72.1", latest="0.72.2",
                      download_url="http://s/agnes-0.72.2.whl")

    ok = su._spawn_windows_deferred_update(info, {}, quiet=True)
    assert ok is True
    argv = captured["argv"]
    assert argv[0] == r"C:\base\python.exe"
    assert argv[2] == str(os.getpid())
    assert argv[4] == "0.72.2"       # expected version
    assert argv[-1] == ""            # no rollback wheel (first upgrade)
    assert captured["kw"]["creationflags"] & 0x00000008  # DETACHED_PROCESS
    assert (tmp_path / "wheels" / "0.72.2.whl").exists()  # wheel staged for the helper


@pytest.mark.no_routing_override
def test_spawn_windows_deferred_update_no_interpreter_fails_safe(monkeypatch, tmp_path):
    from cli.commands import self_upgrade as su
    monkeypatch.setattr("cli.commands.self_upgrade.shutil.which", lambda n: "uv.exe")
    monkeypatch.setattr(su, "_helper_interpreter", lambda: None)  # no safe interpreter
    called = {"popen": 0}
    monkeypatch.setattr("cli.commands.self_upgrade.subprocess.Popen",
                        lambda *a, **k: called.__setitem__("popen", 1))
    info = UpdateInfo(installed="0.72.1", latest="0.72.2", download_url="http://s/x.whl")
    assert su._spawn_windows_deferred_update(info, {}, quiet=True) is False
    assert called["popen"] == 0  # never spawned


def test_staged_wheel_name_keeps_real_pep427_filename():
    # uv tool install parses the wheel filename; the server's download_url ends
    # in the valid `name-version-tags.whl`, so we stage under THAT name — a bare
    # `<version>.whl` is rejected ("Must have a version") and broke every
    # Windows deferred update.
    from cli.commands.self_upgrade import _staged_wheel_name

    url = "https://s.example/cli/wheel/agnes_the_ai_analyst-0.72.4-py3-none-any.whl"
    assert _staged_wheel_name(url, "0.72.4") == "agnes_the_ai_analyst-0.72.4-py3-none-any.whl"


def test_staged_wheel_name_falls_back_on_degenerate_url():
    from cli.commands.self_upgrade import _staged_wheel_name

    # Missing URL, or a URL that ends in the invalid bare `<version>.whl`, both
    # fall back to a constructed PEP 427-valid name.
    assert _staged_wheel_name(None, "0.72.4") == "agnes_the_ai_analyst-0.72.4-py3-none-any.whl"
    assert (
        _staged_wheel_name("https://s.example/cli/wheel/0.72.4.whl", "0.72.4")
        == "agnes_the_ai_analyst-0.72.4-py3-none-any.whl"
    )
