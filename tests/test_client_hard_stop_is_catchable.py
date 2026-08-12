"""A hard stop must not kill callers that handle their own failures.

`cli/client.py` detects two conditions in an httpx *response event hook* —
a redirect the CLI will not follow, and a version floor the server refuses
to serve. Both used to end the process from inside the hook with
`sys.stderr.write` + `sys.exit(2)`, deep inside somebody else's
`api_get(...)`.

`SystemExit` derives from `BaseException`, so it walks straight through
`except Exception`. Two callers are built precisely to survive a failed
request, and both were silently voided:

- `agnes diagnose` records a row per failed check. Measured against the real
  relocated hostname on 2026-08-12 — an unreachable server gives exit 0 and
  the full JSON checklist, a redirect gave exit 2, empty stdout and not one
  check, from the command whose entire job is to say what is wrong.
- `agnes update` wraps every step so one failure cannot abort the run.
  `_run_step` did not contain it either, so the process died before the
  report was written.

The fix raises `AgnesHardStop` instead. The user-visible contract is
unchanged — `cli/main.py` prints the same `error: …` line to stderr and
exits with the same code — which the last class here pins, because a
"catchable now" change that quietly reworded or renumbered the failure
would break scripts to fix aggregators.
"""

from __future__ import annotations

import json

import httpx
import pytest
from typer.testing import CliRunner

from cli.client import AgnesHardStop, _check_moved_server

runner = CliRunner()


def _redirect(status: int = 308, location: str = "https://new.example/api/health") -> httpx.Response:
    return httpx.Response(
        status_code=status,
        headers={"Location": location},
        request=httpx.Request("GET", "https://old.example/api/health"),
    )


@pytest.fixture
def moved_server(monkeypatch):
    """Every `api_get` reachable from `diagnose` meets a server that has moved.

    Patched at the USE site, not only at the definition site.
    `cli/commands/diagnose.py` does `from cli.client import api_get` at module
    scope, so it holds its own binding and never sees a patch of
    `cli.client.api_get`. Patching only the latter made these tests pass or
    fail on import order alone: run by themselves they were green, because
    the diagnose module was first imported *inside* the test, after the
    patch — put `tests/test_cli_diagnose.py` in front and the module was
    already in `sys.modules` with the real function bound, and the redirect
    silently became "Can't reach the agnes server".
    """
    monkeypatch.setattr("cli.client.get_server_url", lambda: "https://old.example")

    def _raise(*_a, **_kw):
        _check_moved_server(_redirect())

    import cli.commands.diagnose  # noqa: F401 — ensure the binding exists to patch

    monkeypatch.setattr("cli.client.api_get", _raise)
    monkeypatch.setattr("cli.commands.diagnose.api_get", _raise)
    return _raise


class TestOrdinaryExceptionHandlingSeesIt:
    def test_it_does_not_escape_as_a_baseexception(self):
        """`SystemExit` was the whole defect: it bypasses `except Exception`."""
        caught = None
        try:
            _check_moved_server(_redirect())
        except Exception as exc:
            caught = exc
        except BaseException as exc:  # noqa: B036 — asserting it does NOT land here
            pytest.fail(f"still escapes ordinary handling as {type(exc).__name__}")
        assert isinstance(caught, AgnesHardStop)

    def test_the_message_still_carries_the_remedy(self):
        with pytest.raises(AgnesHardStop) as exc:
            _check_moved_server(_redirect())
        assert "new.example" in exc.value.user_message
        assert "AGNES_SERVER" in exc.value.user_message


class TestDiagnoseStillDiagnoses:
    """The command that exists to report failures must report this one."""

    def _run(self, args: list[str]):
        from cli.commands.diagnose import diagnose_app

        return runner.invoke(diagnose_app, args)

    def test_it_produces_a_checklist_instead_of_dying(self, moved_server):
        result = self._run(["--json"])
        assert result.stdout.strip(), "a moved server produced no output at all"
        payload = json.loads(result.stdout)
        assert payload["checks"], "not a single check ran"

    def test_the_api_check_names_where_the_server_went(self, moved_server):
        payload = json.loads(self._run(["--json"]).stdout)
        api = next(c for c in payload["checks"] if c["name"] == "api")
        assert api["status"] == "error"
        assert "new.example" in api["detail"]

    def test_the_other_checks_still_run(self, moved_server):
        """One dead check must not take the local-side checks with it."""
        payload = json.loads(self._run(["--json"]).stdout)
        assert {c["name"] for c in payload["checks"]} - {"api"}, "only the failing check survived"


class TestUpdateStepIsolationHolds:
    """`_run_step` exists so one bad step cannot abort a convergence run."""

    def test_a_hard_stop_is_contained_as_an_error_row(self):
        from cli.commands.update import _run_step

        report: list[dict] = []
        _run_step("probe", lambda: _check_moved_server(_redirect()), report)
        assert report, "the step took the whole run down instead of reporting"
        assert report[0]["status"] == "error"


class TestTheUserFacingContractIsUnchanged:
    """Catchable is the only thing that changed. Not the text, not the code."""

    def test_it_still_exits_2(self):
        with pytest.raises(AgnesHardStop) as exc:
            _check_moved_server(_redirect())
        assert exc.value.exit_code == 2

    def test_main_renders_it_exactly_as_the_hook_used_to(self, monkeypatch, capsys):
        """Rendering moved from the hook to `cli/main.py`; the bytes did not.

        Drives the real `main()` — asserting against a hand-rolled copy of
        its handler would only prove the copy.
        """
        import cli.main as m

        def boom():
            raise AgnesHardStop("old.example answered HTTP 308 (redirect to https://new.example/x)")

        monkeypatch.setattr(m, "app", boom)
        with pytest.raises(SystemExit) as exit_exc:
            m.main()
        assert exit_exc.value.code == 2, "the exit code scripts read has changed"
        err = capsys.readouterr().err
        assert err.startswith("error: "), f"prefix changed: {err[:40]!r}"
        assert "new.example" in err

    def test_a_hard_stop_is_not_forwarded_to_telemetry(self, monkeypatch):
        """It was a `SystemExit`, which this wrapper never reported.

        Making it catchable must not also start a new telemetry stream as a
        side effect — that is a separate decision, not a refactor.
        """
        import cli.main as m

        captured: list = []
        monkeypatch.setattr(m, "_capture_cli_exception", lambda *a, **k: captured.append(a))
        monkeypatch.setattr(m, "app", lambda: (_ for _ in ()).throw(AgnesHardStop("moved")))
        with pytest.raises(SystemExit):
            m.main()
        assert captured == [], "a structural fix quietly started reporting to telemetry"
