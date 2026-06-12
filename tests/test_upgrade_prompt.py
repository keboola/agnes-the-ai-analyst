"""Tests for the one-time interactive upgrade prompt on version drift (#617).

Covers:
- the pure decision function (behind + TTY + not-bypassed + no-skip-state +
  not-re-exec'd → prompt; any gate off → no prompt),
- decline writes skip-state-<server-version>; a subsequent call with the
  file present does NOT prompt,
- `--no-update-check` and `AGNES_NO_UPDATE_CHECK=1` skip the prompt,
- non-TTY skips (mock isatty False),
- the `self-update` alias resolves to the same callback as `self-upgrade`,
- re-exec: decision + os.execv mocked (asserts called with original argv on
  accept); we never actually exec.
"""

import os
from unittest.mock import MagicMock, patch

import pytest

from cli.update_check import UpdateInfo


@pytest.fixture(autouse=True)
def _isolate_config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_agnes_cfg"))
    # Ensure neither gate env var leaks in from the host.
    monkeypatch.delenv("AGNES_NO_UPDATE_CHECK", raising=False)
    monkeypatch.delenv("AGNES_UPGRADE_PROMPTED", raising=False)
    yield


def _behind() -> UpdateInfo:
    return UpdateInfo(
        installed="2.0.0",
        latest="2.3.0",
        download_url="http://server.test/cli/wheel/agnes-2.3.0.whl",
    )


def _current() -> UpdateInfo:
    return UpdateInfo(installed="2.3.0", latest="2.3.0", download_url="x")


# ---------------------------------------------------------------------------
# Pure decision function
# ---------------------------------------------------------------------------

def test_decision_prompts_when_all_gates_open():
    from cli.upgrade_prompt import should_prompt_upgrade

    assert should_prompt_upgrade(
        _behind(), isatty=True, bypassed=False,
        skip_present=False, sentinel_set=False,
    ) is True


def test_decision_no_prompt_when_current():
    from cli.upgrade_prompt import should_prompt_upgrade

    assert should_prompt_upgrade(
        _current(), isatty=True, bypassed=False,
        skip_present=False, sentinel_set=False,
    ) is False


def test_decision_no_prompt_when_info_none():
    from cli.upgrade_prompt import should_prompt_upgrade

    assert should_prompt_upgrade(
        None, isatty=True, bypassed=False,
        skip_present=False, sentinel_set=False,
    ) is False


def test_decision_no_prompt_when_not_tty():
    from cli.upgrade_prompt import should_prompt_upgrade

    assert should_prompt_upgrade(
        _behind(), isatty=False, bypassed=False,
        skip_present=False, sentinel_set=False,
    ) is False


def test_decision_no_prompt_when_bypassed():
    from cli.upgrade_prompt import should_prompt_upgrade

    assert should_prompt_upgrade(
        _behind(), isatty=True, bypassed=True,
        skip_present=False, sentinel_set=False,
    ) is False


def test_decision_no_prompt_when_skip_state_present():
    from cli.upgrade_prompt import should_prompt_upgrade

    assert should_prompt_upgrade(
        _behind(), isatty=True, bypassed=False,
        skip_present=True, sentinel_set=False,
    ) is False


def test_decision_no_prompt_when_reexec_sentinel_set():
    from cli.upgrade_prompt import should_prompt_upgrade

    assert should_prompt_upgrade(
        _behind(), isatty=True, bypassed=False,
        skip_present=False, sentinel_set=True,
    ) is False


# ---------------------------------------------------------------------------
# Bypass gate (env var + flag)
# ---------------------------------------------------------------------------

def test_is_bypassed_env_var(monkeypatch):
    from cli.upgrade_prompt import is_bypassed

    monkeypatch.setenv("AGNES_NO_UPDATE_CHECK", "1")
    assert is_bypassed([]) is True


def test_is_bypassed_flag():
    from cli.upgrade_prompt import is_bypassed

    assert is_bypassed(["pull", "--no-update-check"]) is True
    assert is_bypassed(["pull"]) is False


# ---------------------------------------------------------------------------
# Skip-state file
# ---------------------------------------------------------------------------

def test_write_and_detect_skip_state():
    from cli.upgrade_prompt import (
        skip_state_path,
        skip_state_present,
        write_skip_state,
    )

    assert skip_state_present("2.3.0") is False
    write_skip_state("2.3.0")
    assert skip_state_present("2.3.0") is True
    # filename is keyed on the server version
    assert skip_state_path("2.3.0").name == "skipped-upgrade-2.3.0"
    # a newer server version is NOT covered → prompt re-arms
    assert skip_state_present("2.4.0") is False


# ---------------------------------------------------------------------------
# maybe_prompt_and_upgrade — orchestration
# ---------------------------------------------------------------------------

def test_decline_writes_skip_state_and_returns_false(monkeypatch):
    from cli import upgrade_prompt

    monkeypatch.setattr(upgrade_prompt, "_stdin_isatty", lambda: True)
    monkeypatch.setattr(upgrade_prompt, "_read_yn_with_timeout", lambda t: False)
    run_su = MagicMock()
    monkeypatch.setattr(upgrade_prompt, "_run_self_upgrade", run_su)

    handled = upgrade_prompt.maybe_prompt_and_upgrade(_behind())

    assert handled is False  # caller falls back to the banner
    run_su.assert_not_called()  # declined → no install
    assert upgrade_prompt.skip_state_present("2.3.0") is True


def test_subsequent_call_with_skip_state_does_not_prompt(monkeypatch):
    from cli import upgrade_prompt

    monkeypatch.setattr(upgrade_prompt, "_stdin_isatty", lambda: True)
    upgrade_prompt.write_skip_state("2.3.0")  # decline already recorded

    read = MagicMock()
    monkeypatch.setattr(upgrade_prompt, "_read_yn_with_timeout", read)

    handled = upgrade_prompt.maybe_prompt_and_upgrade(_behind())

    assert handled is False
    read.assert_not_called()  # never even prompted


def test_non_tty_skips_prompt(monkeypatch):
    from cli import upgrade_prompt

    monkeypatch.setattr(upgrade_prompt, "_stdin_isatty", lambda: False)
    read = MagicMock()
    monkeypatch.setattr(upgrade_prompt, "_read_yn_with_timeout", read)

    handled = upgrade_prompt.maybe_prompt_and_upgrade(_behind())

    assert handled is False
    read.assert_not_called()
    # non-TTY must NOT write skip-state (banner stays as the fallback)
    assert upgrade_prompt.skip_state_present("2.3.0") is False


def test_env_var_bypass_skips_prompt(monkeypatch):
    from cli import upgrade_prompt

    monkeypatch.setenv("AGNES_NO_UPDATE_CHECK", "1")
    monkeypatch.setattr(upgrade_prompt, "_stdin_isatty", lambda: True)
    read = MagicMock()
    monkeypatch.setattr(upgrade_prompt, "_read_yn_with_timeout", read)

    handled = upgrade_prompt.maybe_prompt_and_upgrade(_behind())

    assert handled is False
    read.assert_not_called()


def test_accept_runs_upgrade_and_reexecs_with_original_argv(monkeypatch):
    from cli import upgrade_prompt

    monkeypatch.setattr(upgrade_prompt, "_stdin_isatty", lambda: True)
    monkeypatch.setattr(upgrade_prompt, "_read_yn_with_timeout", lambda t: True)
    run_su = MagicMock()
    monkeypatch.setattr(upgrade_prompt, "_run_self_upgrade", run_su)

    fake_binary = "/home/u/.local/bin/agnes"
    monkeypatch.setattr(
        "cli.commands.self_upgrade._uv_tool_bin_path",
        lambda: __import__("pathlib").Path(fake_binary),
    )
    monkeypatch.setattr(
        "cli.commands.self_upgrade._pip_bin_path", lambda: None
    )

    # Simulate the user's original command.
    monkeypatch.setattr("sys.argv", ["agnes", "pull", "--quiet"])
    execv = MagicMock()
    monkeypatch.setattr("os.execv", execv)

    handled = upgrade_prompt.maybe_prompt_and_upgrade(_behind())

    run_su.assert_called_once()  # upgrade ran before re-exec
    execv.assert_called_once()
    path_arg, argv_arg = execv.call_args[0]
    assert path_arg == fake_binary
    # original argv preserved, binary path prepended
    assert argv_arg == [fake_binary, "pull", "--quiet"]
    # re-exec sentinel set so the child never re-prompts / loops
    assert os.environ.get("AGNES_UPGRADE_PROMPTED") == "1"
    # exec was mocked (didn't replace the process) → we reported handled
    assert handled is True


def test_timeout_defaults_to_accept_and_upgrades(monkeypatch):
    """5s timeout (no input) is treated as Y → run upgrade + re-exec."""
    from cli import upgrade_prompt

    monkeypatch.setattr(upgrade_prompt, "_stdin_isatty", lambda: True)
    # _read_yn_with_timeout returns True on timeout per its contract.
    monkeypatch.setattr(upgrade_prompt, "_read_yn_with_timeout", lambda t: True)
    run_su = MagicMock()
    monkeypatch.setattr(upgrade_prompt, "_run_self_upgrade", run_su)
    monkeypatch.setattr(
        "cli.commands.self_upgrade._uv_tool_bin_path",
        lambda: __import__("pathlib").Path("/bin/agnes"),
    )
    monkeypatch.setattr("cli.commands.self_upgrade._pip_bin_path", lambda: None)
    monkeypatch.setattr("sys.argv", ["agnes", "catalog"])
    monkeypatch.setattr("os.execv", MagicMock())

    upgrade_prompt.maybe_prompt_and_upgrade(_behind())
    run_su.assert_called_once()


# ---------------------------------------------------------------------------
# self-update alias
# ---------------------------------------------------------------------------

def test_self_update_alias_resolves_to_same_callback_as_self_upgrade():
    from cli.commands.self_upgrade import self_upgrade_app
    from cli.main import app

    groups = {g.name: g for g in app.registered_groups}
    assert "self-upgrade" in groups
    assert "self-update" in groups
    # Both names point at the exact same Typer instance → same callback.
    assert groups["self-upgrade"].typer_instance is self_upgrade_app
    assert groups["self-update"].typer_instance is self_upgrade_app


def test_self_update_alias_is_hidden_but_invokable():
    from cli.main import app

    groups = {g.name: g for g in app.registered_groups}
    assert groups["self-update"].hidden is True
    # canonical verb stays visible — `hidden` is an unset DefaultPlaceholder
    # there, which Typer resolves to falsy (the command shows in --help).
    assert not bool(groups["self-upgrade"].hidden)


# ---------------------------------------------------------------------------
# Devin Review fixes on #619
# ---------------------------------------------------------------------------

def test_eof_on_prompt_returns_no_not_yes(monkeypatch):
    """Regression — Devin Review ANALYSIS_0003 on #619.

    `readline()` returns `""` on EOF (Ctrl+D / closed stdin) and `"\\n"`
    on bare Enter. The two must be distinguishable — Ctrl+D used to fall
    through the "answer == 'n'" check and return True, silently auto-
    triggering an upgrade. EOF now returns False so the prompt defers
    to a later run."""
    from cli import upgrade_prompt

    class _FakeStdin:
        def fileno(self):
            return 0

        def isatty(self):
            return True

        def readline(self):
            return ""  # EOF

    monkeypatch.setattr("sys.stdin", _FakeStdin())
    # select.select returns ready immediately so we exercise the EOF path
    # rather than the timeout path.
    monkeypatch.setattr("select.select", lambda r, w, x, t: (r, [], []))
    assert upgrade_prompt._read_yn_with_timeout(5) is False


def test_run_self_upgrade_returns_false_on_install_failure(monkeypatch):
    """Regression — Devin Review BUG_0001 on #619.

    `_do_install_with_smoke_and_rollback` returns 1 on install error or
    smoke-test rollback. The wrapper used to discard that rc, so the
    caller would proceed to print `[upgraded → …]` and re-exec the
    still-stale binary. The fix returns False so the caller skips both."""
    from cli import upgrade_prompt
    from cli.update_check import UpdateInfo

    info = UpdateInfo(installed="0.0.1", latest="9.9.9", download_url="x")
    monkeypatch.setattr("cli.commands.self_upgrade._resolve_info", lambda force: info)
    install_mock = MagicMock(return_value=1)  # install failure
    monkeypatch.setattr(
        "cli.commands.self_upgrade._do_install_with_smoke_and_rollback",
        install_mock,
    )
    record = MagicMock()
    monkeypatch.setattr("cli.upgrade_status.record_outcome", record)
    refresh = MagicMock()
    monkeypatch.setattr(
        "cli.commands.self_upgrade._try_refresh_hooks", refresh
    )

    assert upgrade_prompt._run_self_upgrade() is False
    install_mock.assert_called_once()
    # Failure counter must still be incremented (matches the canonical
    # self_upgrade callback's wiring, ANALYSIS_0001).
    record.assert_called_once_with(success=False)
    # Hook refresh skipped on failure — only a successful install warrants it.
    refresh.assert_not_called()


def test_run_self_upgrade_records_success_and_refreshes_hooks_on_clean_install(monkeypatch):
    """Regression — Devin Review ANALYSIS_0001 on #619.

    The wrapper must mirror the `self_upgrade` CLI callback's post-install
    wiring: `record_outcome(success=True)` (resets the #478 consecutive-
    failure counter) and `_try_refresh_hooks(quiet=False)` (wire-format
    changes in the new release land on the next session-start)."""
    from cli import upgrade_prompt
    from cli.update_check import UpdateInfo

    info = UpdateInfo(installed="0.0.1", latest="9.9.9", download_url="x")
    monkeypatch.setattr("cli.commands.self_upgrade._resolve_info", lambda force: info)
    monkeypatch.setattr(
        "cli.commands.self_upgrade._do_install_with_smoke_and_rollback",
        MagicMock(return_value=0),  # clean install
    )
    record = MagicMock()
    monkeypatch.setattr("cli.upgrade_status.record_outcome", record)
    refresh = MagicMock()
    monkeypatch.setattr(
        "cli.commands.self_upgrade._try_refresh_hooks", refresh
    )

    assert upgrade_prompt._run_self_upgrade() is True
    record.assert_called_once_with(success=True)
    refresh.assert_called_once_with(quiet=False)
