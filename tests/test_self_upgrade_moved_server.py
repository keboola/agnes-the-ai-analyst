"""`agnes self-upgrade` must diagnose a moved server, not go silent.

Measured against the real relocated hostname on 2026-08-12:

    $ AGNES_SERVER=<old> agnes self-upgrade
    (nothing, exit 0)

    $ AGNES_SERVER=<old> agnes self-upgrade --force
    agnes self-upgrade: cannot reach <old>/cli/latest

The server is reachable — `GET /cli/latest` answers `308` with a `Location`
pointing at the new host. So the plain form is a silent no-op (the caller
cannot tell the upgrade did not happen) and `--force` gives a diagnosis that
is simply untrue.

Cause: `cli/update_check.py::_fetch_latest` calls `raise_for_status()`,
which does not treat 3xx as an error, then `.json()` on a redirect's empty
body; the resulting exception is swallowed into `None`, which
`_resolve_info` maps to "probe failed".

This matters because `self-upgrade` is what someone runs to FIX a stale
install — `agnes catalog` and `agnes update` already name the new address
(#1266), so this was the one remaining way to hit the moved server and be
told nothing useful.

The SessionStart hook runs `--quiet` and must stay silent: that path is
non-noisy by contract (#601), so the new message is gated on it.
"""

from __future__ import annotations

import httpx
import pytest
from typer.testing import CliRunner

from cli.main import app
from cli.upgrade_status import _WARN_THRESHOLD, consecutive_failures, read_status, should_warn

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("AGNES_SELF_UPGRADE_IN_PROGRESS", raising=False)


#: Captured BEFORE any patching. A factory that calls `httpx.Client` after
#: that attribute is monkeypatched calls ITSELF — infinite recursion, which
#: the probe's `except Exception` swallows into a silent `None`, making a
#: working fix look broken when only the test was.
_REAL_HTTPX_CLIENT = httpx.Client


def _client_answering(status: int, location: str):
    """An httpx.Client factory whose every GET answers one fixed redirect."""

    def factory(*_a, **_kw):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, headers={"Location": location}, request=request)

        return _REAL_HTTPX_CLIENT(transport=httpx.MockTransport(handler), base_url="https://old.example")

    return factory


#: The relocated-deployment case: the old hostname answers 308 across hosts.
_redirecting_client = _client_answering(308, "https://new.example/cli/latest")


def _install_redirecting_server(monkeypatch, client_factory):
    # `httpx` is imported INSIDE `_fetch_latest`, so it is not an attribute
    # of the module — patch the library itself.
    # `check` returns None on ANY probe failure — that is exactly what a 308
    # produces in real life (raise_for_status ignores 3xx, .json() then trips
    # on the empty body, and the exception is swallowed).
    monkeypatch.setattr("cli.commands.self_upgrade.get_server_url", lambda: "https://old.example")
    monkeypatch.setattr("cli.commands.self_upgrade.check", lambda *a, **k: None)
    monkeypatch.setattr("httpx.Client", client_factory)


@pytest.fixture
def moved_server(monkeypatch):
    _install_redirecting_server(monkeypatch, _redirecting_client)


@pytest.fixture
def proxy_bounced_server(monkeypatch):
    """A SAME-ORIGIN redirect — an SSO proxy answering `302 /login`.

    Not a move: there is no new address to hand over. But the server
    answered, so `cannot reach` is just as untrue here, and silence just as
    unhelpful. This case exists because the verdict keys off "is a redirect",
    not "is a move" (Devin Review on #1275) — pinned so that stays deliberate.
    """
    _install_redirecting_server(monkeypatch, _client_answering(302, "/login"))


class TestMovedServerIsDiagnosed:
    def test_plain_self_upgrade_is_not_silent(self, moved_server):
        result = runner.invoke(app, ["self-upgrade"])
        assert result.output.strip(), "a moved server produced no output at all"
        assert "new.example" in result.output, "does not name where the server moved"

    def test_it_does_not_claim_the_server_is_unreachable(self, moved_server):
        """The server answered. Saying otherwise sends the user debugging DNS."""
        result = runner.invoke(app, ["self-upgrade", "--force"])
        assert "cannot reach" not in result.output.lower(), (
            "still reports an unreachable server for one that answered 308"
        )
        assert "new.example" in result.output

    def test_the_message_carries_a_remedy(self, moved_server):
        result = runner.invoke(app, ["self-upgrade"])
        assert "AGNES_SERVER" in result.output, "no way forward offered"

    def test_exit_code_is_non_zero_so_a_script_notices(self, moved_server):
        result = runner.invoke(app, ["self-upgrade"])
        assert result.exit_code != 0, "a no-op upgrade reported success"


class TestARedirectThatIsNotAMove:
    """Every redirect is a check that did not happen — not only a move."""

    def test_a_same_origin_bounce_is_still_reported(self, proxy_bounced_server):
        result = runner.invoke(app, ["self-upgrade"])
        assert result.exit_code != 0, "a check that never ran reported success"
        assert "302" in result.output, "does not say what the server answered"

    def test_it_does_not_claim_a_move_it_cannot_support(self, proxy_bounced_server):
        """There is no new address here. Naming one would be an invention."""
        result = runner.invoke(app, ["self-upgrade"])
        # Assert there IS a message first: "makes no false claim" is satisfied
        # by saying nothing at all, and silence is the bug, not the fix.
        assert "old.example" in result.output, "nothing was reported to inspect"
        assert "has moved" not in result.output.lower()
        assert "cannot reach" not in result.output.lower(), "the server answered"


class TestTheQuietContractHolds:
    def test_quiet_stays_silent_on_a_moved_server(self, moved_server):
        """SessionStart runs this on every shell; it must not start shouting."""
        result = runner.invoke(app, ["self-upgrade", "--quiet"])
        assert result.exit_code == 0
        assert "new.example" not in result.output


class TestCheckOnlySaysSomething:
    """`--check-only` swallows every other transport verdict into exit 0.

    A redirect must not join them: "up to date" about a check that never ran
    is the same silent lie in a different costume (Devin Review on #1275).
    """

    def test_check_only_reports_the_redirect(self, moved_server):
        result = runner.invoke(app, ["self-upgrade", "--check-only"])
        assert result.exit_code == 1
        assert "new.example" in result.output

    def test_the_help_text_admits_this_second_exit_1(self):
        """It used to promise exit 1 meant `outdated`, and only that."""
        result = runner.invoke(app, ["self-upgrade", "--help"])
        assert "redirect" in result.output.lower(), "--check-only still documents exit 1 as 'outdated' alone"


class TestTheQuietPathIsNotInvisibleForever:
    """The quiet path prints nothing — so the #478 counter is the only channel.

    Without this the analyst's SessionStart hook fails identically on every
    shell, forever, and nothing ever says so: the stale CLI this command
    exists to repair, made permanent. A redirect is counted (unlike
    `_Offline`) because it is deterministic, not a transient blip that would
    raise a false alarm.
    """

    def test_a_quiet_run_records_the_failure(self, moved_server):
        runner.invoke(app, ["self-upgrade", "--quiet"])
        assert consecutive_failures() == 1, "a silent no-op left no trace at all"

    def test_the_recorded_reason_names_the_new_address(self, moved_server):
        runner.invoke(app, ["self-upgrade", "--quiet"])
        reason = read_status().get("last_failure_reason", "")
        assert "new.example" in reason, f"warning would say nothing useful: {reason!r}"

    def test_repeated_quiet_runs_eventually_surface_a_warning(self, moved_server):
        for _ in range(_WARN_THRESHOLD):
            runner.invoke(app, ["self-upgrade", "--quiet"])
        assert should_warn(), f"{_WARN_THRESHOLD} identical silent failures still warn nobody"

    def test_the_noisy_path_records_it_too(self, moved_server):
        runner.invoke(app, ["self-upgrade"])
        assert consecutive_failures() == 1


class TestAGenuinelyDeadServerIsUnchanged:
    def test_connection_failure_still_reports_unreachable(self, monkeypatch):
        def dead(*_a, **_kw):
            def handler(request: httpx.Request) -> httpx.Response:
                raise httpx.ConnectError("no route to host", request=request)

            return _REAL_HTTPX_CLIENT(transport=httpx.MockTransport(handler), base_url="https://dead.example")

        monkeypatch.setattr("cli.commands.self_upgrade.get_server_url", lambda: "https://dead.example")
        monkeypatch.setattr("cli.commands.self_upgrade.check", lambda *a, **k: None)
        monkeypatch.setattr("httpx.Client", dead)

        result = runner.invoke(app, ["self-upgrade", "--force"])
        assert "cannot reach" in result.output.lower(), "a real connection failure lost its diagnosis"
