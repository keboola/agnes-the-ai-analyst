"""The auto-mode trust declaration: what it says, and whether it was asked for.

Two independent defects, both reported by an agent reading its own
configuration after `agnes init` wrote to it.

1. The entries argued a conclusion ("a routine, sanctioned internal
   operation, not integration of untrusted external code") instead of stating
   a fact. A tool writing a verdict about itself into the classifier's input
   is the pattern an agent is supposed to distrust, whatever the tool.
2. It was written to `~/.claude/settings.json` — outside the workspace, for
   every project on the machine — with no consent and only a line printed
   afterwards.

The mechanism itself is fine and stays: `autoMode.environment` is the
sanctioned channel, and using it beats arguing in prose. These tests pin the
wording and the three consent paths.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from cli.lib.automode import TrustResult, ensure_marketplace_trusted, marketplace_trust_entries

runner = CliRunner()

HOST = "agnes.example.com"

# Words that assert a conclusion about how the reader should judge an action,
# rather than describing the environment.
_VERDICT_WORDS = (
    "sanctioned",
    "routine",
    "untrusted external",
    "not integration",
    "safe to",
    "no risk",
    "you can trust",
    "should be trusted",
)


class TestWording:
    def test_entries_state_facts_and_do_not_argue(self):
        joined = " ".join(marketplace_trust_entries(HOST)).lower()
        offenders = [w for w in _VERDICT_WORDS if w in joined]
        assert not offenders, f"trust entries argue a conclusion: {offenders}"

    def test_entries_still_identify_the_host_and_the_registry(self):
        """Stripping the argument must not strip the information."""
        entries = marketplace_trust_entries(HOST)
        joined = " ".join(entries)
        assert len(entries) == 2
        assert HOST in joined
        assert f"https://{HOST}/marketplace.git/" in joined
        # The recognized trust-slot labels are what make the classifier read
        # these as environment facts rather than free-form context.
        assert entries[0].startswith("Trusted internal domains:")
        assert entries[1].startswith("Internal package registry:")

    def test_host_is_never_hardcoded(self):
        """Vendor-agnostic repo: the host always comes from configuration."""
        joined = " ".join(marketplace_trust_entries("other.internal"))
        assert "other.internal" in joined
        assert "example.com" not in joined


class TestMergeBehaviour:
    def test_writes_entries_and_keeps_defaults(self, tmp_path):
        settings = tmp_path / "settings.json"
        assert ensure_marketplace_trusted(settings, HOST) is TrustResult.WRITTEN
        data = json.loads(settings.read_text())
        # "$defaults" must survive — dropping it replaces the whole built-in
        # rule list for that section.
        assert data["autoMode"]["environment"][0] == "$defaults"
        assert data["autoMode"]["environment"][1:] == marketplace_trust_entries(HOST)

    def test_preserves_unrelated_keys(self, tmp_path):
        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({"model": "opus", "statusLine": {"type": "command"}}))
        ensure_marketplace_trusted(settings, HOST)
        data = json.loads(settings.read_text())
        assert data["model"] == "opus"
        assert data["statusLine"] == {"type": "command"}

    def test_idempotent(self, tmp_path):
        settings = tmp_path / "settings.json"
        assert ensure_marketplace_trusted(settings, HOST) is TrustResult.WRITTEN
        assert ensure_marketplace_trusted(settings, HOST) is TrustResult.ALREADY_PRESENT
        assert len(json.loads(settings.read_text())["autoMode"]["environment"]) == 3

    def test_corrupt_settings_are_never_overwritten(self, tmp_path):
        settings = tmp_path / "settings.json"
        settings.write_text("{not json")
        assert ensure_marketplace_trusted(settings, HOST) is TrustResult.NOT_WRITTEN
        assert settings.read_text() == "{not json"

    def test_empty_host_is_a_noop(self, tmp_path):
        settings = tmp_path / "settings.json"
        assert ensure_marketplace_trusted(settings, "") is TrustResult.NOT_WRITTEN
        assert not settings.exists()


# --------------------------------------------------------------------------
# Consent. `agnes init` is invited into a workspace; this write leaves it.
# --------------------------------------------------------------------------


def _make_api_get():
    def _api_get(path, *args, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        if path == "/api/catalog/tables":
            resp.json.return_value = []
        elif path == "/api/welcome":
            resp.json.return_value = {"content": "# Workspace\n"}
        else:
            resp.json.return_value = {}
        return resp

    return _api_get


@pytest.fixture
def init_env(tmp_path, monkeypatch):
    """Run `agnes init` against a fake home, with a known marketplace host."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_cfg"))
    monkeypatch.setenv("AGNES_NO_UPDATE_CHECK", "1")

    api_get = _make_api_get()
    monkeypatch.setattr("cli.commands.init.api_get", api_get, raising=False)
    monkeypatch.setattr("cli.lib.pull.api_get", api_get, raising=False)
    # `initial_workspace` imports its own `api_get`; without this the
    # connector-params probe makes a real DNS lookup mid-test.
    monkeypatch.setattr("cli.lib.initial_workspace.api_get", api_get, raising=False)
    monkeypatch.setattr("cli.commands.init.configured_marketplace_host", lambda: HOST, raising=False)

    settings = home / ".claude" / "settings.json"
    monkeypatch.setattr("cli.commands.init.user_settings_path", lambda: settings, raising=False)

    workspace = tmp_path / "ws"
    workspace.mkdir()
    return {"settings": settings, "workspace": workspace}


def _run_init(workspace, *extra):
    from cli.commands.init import init_app

    return runner.invoke(
        init_app,
        [
            "--server-url",
            "http://test.example.com",
            "--token",
            "test-pat",
            "--workspace",
            str(workspace),
            *extra,
        ],
    )


def _declared(settings_path) -> bool:
    if not settings_path.exists():
        return False
    env = json.loads(settings_path.read_text()).get("autoMode", {}).get("environment", [])
    return any(HOST in e for e in env if isinstance(e, str))


class TestConsent:
    def test_unattended_run_does_not_touch_user_settings(self, init_env, monkeypatch):
        """The pasted-install path: nobody is there to agree."""
        monkeypatch.setattr("cli.commands.init._stdin_is_interactive", lambda: False)
        result = _run_init(init_env["workspace"])
        assert result.exit_code == 0, result.output
        assert not _declared(init_env["settings"])
        # And it says so, with the way to opt in — silence would just move the
        # surprise to whoever wonders why auto mode is asking.
        assert "--trust-marketplace-host" in result.output

    def test_explicit_flag_declares_without_asking(self, init_env, monkeypatch):
        monkeypatch.setattr("cli.commands.init._stdin_is_interactive", lambda: False)
        result = _run_init(init_env["workspace"], "--trust-marketplace-host")
        assert result.exit_code == 0, result.output
        assert _declared(init_env["settings"])

    def test_explicit_refusal_is_honoured_even_interactively(self, init_env, monkeypatch):
        monkeypatch.setattr("cli.commands.init._stdin_is_interactive", lambda: True)
        # A prompt here would be a bug: the operator already answered.
        monkeypatch.setattr(
            "typer.confirm",
            lambda *a, **k: pytest.fail("asked despite --no-trust-marketplace-host"),
        )
        result = _run_init(init_env["workspace"], "--no-trust-marketplace-host")
        assert result.exit_code == 0, result.output
        assert not _declared(init_env["settings"])

    def test_interactive_yes_declares(self, init_env, monkeypatch):
        monkeypatch.setattr("cli.commands.init._stdin_is_interactive", lambda: True)
        monkeypatch.setattr("typer.confirm", lambda *a, **k: True)
        result = _run_init(init_env["workspace"])
        assert result.exit_code == 0, result.output
        assert _declared(init_env["settings"])

    def test_interactive_no_leaves_settings_alone(self, init_env, monkeypatch):
        monkeypatch.setattr("cli.commands.init._stdin_is_interactive", lambda: True)
        monkeypatch.setattr("typer.confirm", lambda *a, **k: False)
        result = _run_init(init_env["workspace"])
        assert result.exit_code == 0, result.output
        assert not _declared(init_env["settings"])

    def test_prompt_shows_the_file_and_the_exact_lines(self, init_env, monkeypatch):
        """Consent to an unseen change is not consent."""
        monkeypatch.setattr("cli.commands.init._stdin_is_interactive", lambda: True)
        monkeypatch.setattr("typer.confirm", lambda *a, **k: False)
        result = _run_init(init_env["workspace"])
        assert "settings.json" in result.output
        assert "every project on this machine" in result.output
        for entry in marketplace_trust_entries(HOST):
            # Typer wraps at the terminal width, so match a distinctive span
            # rather than the whole line.
            assert entry.split(":")[0] in result.output

    def test_declining_never_fails_init(self, init_env, monkeypatch):
        """The declaration is an optimization; the workspace is the product."""
        monkeypatch.setattr("cli.commands.init._stdin_is_interactive", lambda: False)
        result = _run_init(init_env["workspace"])
        assert result.exit_code == 0
        assert (init_env["workspace"] / "CLAUDE.md").exists()


class TestTheReportMatchesWhatWasSaved:
    """Devin Review on #1262: one `False` covered two opposite outcomes.

    "Already declared" and "could not write anything" were reported with the
    same sentence, so an operator who explicitly opted in and hit a malformed
    settings file walked away believing the entries were in place — and later
    wondered why auto mode kept asking.
    """

    def test_a_failed_write_is_not_reported_as_already_declared(self, init_env, monkeypatch):
        monkeypatch.setattr("cli.commands.init._stdin_is_interactive", lambda: True)
        monkeypatch.setattr("typer.confirm", lambda *a, **k: True)
        init_env["settings"].parent.mkdir(parents=True, exist_ok=True)
        init_env["settings"].write_text("{not json")

        result = _run_init(init_env["workspace"])

        assert "was already declared" not in result.output, result.output
        assert "nothing was saved" in result.output, result.output
        assert init_env["settings"].read_text() == "{not json"

    def test_an_entry_that_is_really_there_still_reads_as_already_declared(self, init_env, monkeypatch):
        monkeypatch.setattr("cli.commands.init._stdin_is_interactive", lambda: True)
        monkeypatch.setattr("typer.confirm", lambda *a, **k: True)
        init_env["settings"].parent.mkdir(parents=True, exist_ok=True)
        init_env["settings"].write_text(
            json.dumps({"autoMode": {"environment": ["$defaults", f"Trusted internal domains: {HOST} is ours."]}})
        )

        result = _run_init(init_env["workspace"])

        assert "was already declared" in result.output, result.output
        assert "nothing was saved" not in result.output, result.output

    def test_a_fresh_write_still_reads_as_declared(self, init_env, monkeypatch):
        monkeypatch.setattr("cli.commands.init._stdin_is_interactive", lambda: True)
        monkeypatch.setattr("typer.confirm", lambda *a, **k: True)

        result = _run_init(init_env["workspace"])

        assert "Declared" in result.output, result.output
        assert _declared(init_env["settings"])
