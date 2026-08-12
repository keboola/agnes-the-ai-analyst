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


def _redirecting_client(*_a, **_kw):
    """An httpx.Client whose every GET answers 308, like the old hostname."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            308,
            headers={"Location": "https://new.example/cli/latest"},
            request=request,
        )

    return _REAL_HTTPX_CLIENT(transport=httpx.MockTransport(handler), base_url="https://old.example")


@pytest.fixture
def moved_server(monkeypatch):
    # `httpx` is imported INSIDE `_fetch_latest`, so it is not an attribute
    # of the module — patch the library itself.
    # `check` returns None on ANY probe failure — that is exactly what a 308
    # produces in real life (raise_for_status ignores 3xx, .json() then trips
    # on the empty body, and the exception is swallowed).
    monkeypatch.setattr("cli.commands.self_upgrade.get_server_url", lambda: "https://old.example")
    monkeypatch.setattr("cli.commands.self_upgrade.check", lambda *a, **k: None)
    monkeypatch.setattr("httpx.Client", _redirecting_client)


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


class TestTheQuietContractHolds:
    def test_quiet_stays_silent_on_a_moved_server(self, moved_server):
        """SessionStart runs this on every shell; it must not start shouting."""
        result = runner.invoke(app, ["self-upgrade", "--quiet"])
        assert result.exit_code == 0
        assert "new.example" not in result.output


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
