"""Tests for `agnes init` orchestrator command."""

from typer.testing import CliRunner

# CI-safety: Typer/rich emits ANSI escapes in --help output. Strip before asserts.
_ANSI_RE = __import__("re").compile(r"\x1b\[[0-9;]*m")


def _clean(s: str) -> str:
    return _ANSI_RE.sub("", s)


from cli.commands.init import init_app  # noqa: E402

runner = CliRunner()


def _make_api_get():
    """Build a stub api_get fn that returns canned responses for every endpoint
    `agnes init` and the inner `run_pull` touch.

    Returned closure is suitable for monkeypatching both
    `cli.commands.init.api_get` and `cli.lib.pull.api_get` so the verify-PAT
    call from init AND the manifest+memory-bundle calls from pull all
    succeed in tests.
    """
    from unittest.mock import MagicMock

    def _api_get(path, *args, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        if path == "/api/catalog/tables":
            resp.json.return_value = []
        elif path == "/api/welcome":
            resp.json.return_value = {
                "content": "# Test CLAUDE.md\n\nUse `agnes pull`.\n",
            }
        elif path == "/api/sync/manifest":
            resp.json.return_value = {"tables": {}}
        elif path == "/api/memory/bundle":
            resp.json.return_value = {"mandatory": [], "approved": []}
        else:
            resp.json.return_value = {}
        # raise_for_status is a no-op MagicMock by default — fine for 200s.
        return resp

    return _api_get


def test_init_help():
    result = runner.invoke(init_app, ["--help"])
    assert result.exit_code == 0
    assert "--server-url" in _clean(result.output)
    assert "--token" in _clean(result.output)
    assert "--force" in _clean(result.output)
    assert "--workspace" in _clean(result.output)


def test_init_writes_expected_files(tmp_path, monkeypatch):
    """Mocked end-to-end: init writes CLAUDE.md, settings.json, AGNES_WORKSPACE.md."""
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_cfg"))
    api_get = _make_api_get()
    monkeypatch.setattr("cli.commands.init.api_get", api_get, raising=False)
    monkeypatch.setattr("cli.lib.pull.api_get", api_get, raising=False)

    result = runner.invoke(
        init_app,
        [
            "--server-url",
            "http://test.example.com",
            "--token",
            "test-pat",
            "--workspace",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert (tmp_path / "CLAUDE.md").exists()
    assert "agnes pull" in (tmp_path / "CLAUDE.md").read_text()
    assert (tmp_path / ".claude" / "settings.json").exists()
    assert (tmp_path / ".claude" / "CLAUDE.local.md").exists()
    assert (tmp_path / "AGNES_WORKSPACE.md").exists()
    # run_pull always creates the analytics.duckdb file (load-bearing).
    assert (tmp_path / "user" / "duckdb" / "analytics.duckdb").exists()
    # init anchors the workspace root in config so `agnes push` (and its
    # SessionEnd hook) can find the Claude Code session folder.
    from cli.config import get_workspace_root

    assert get_workspace_root() == str(tmp_path.resolve())


def test_init_no_dead_dirs_zero_grants(tmp_path, monkeypatch):
    """Zero grants -> no .claude/rules, no server/parquet, no user/sessions."""
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_cfg"))
    api_get = _make_api_get()
    monkeypatch.setattr("cli.commands.init.api_get", api_get, raising=False)
    monkeypatch.setattr("cli.lib.pull.api_get", api_get, raising=False)

    runner.invoke(
        init_app,
        [
            "--server-url",
            "http://x",
            "--token",
            "t",
            "--workspace",
            str(tmp_path),
        ],
    )
    for forbidden in [
        "data/parquet",
        "data/duckdb",
        "data/metadata",
        "user/artifacts",
        "user/sessions",
        "server/parquet",
        ".claude/rules",
    ]:
        assert not (tmp_path / forbidden).exists(), f"forbidden created: {forbidden}"


def test_init_force_preserves_local_md(tmp_path, monkeypatch):
    """--force regenerates CLAUDE.md but never touches CLAUDE.local.md."""
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_cfg"))
    api_get = _make_api_get()
    monkeypatch.setattr("cli.commands.init.api_get", api_get, raising=False)
    monkeypatch.setattr("cli.lib.pull.api_get", api_get, raising=False)

    # First init seeds the workspace + writes the default CLAUDE.local.md stub.
    r1 = runner.invoke(
        init_app,
        [
            "--server-url",
            "http://x",
            "--token",
            "t",
            "--workspace",
            str(tmp_path),
        ],
    )
    assert r1.exit_code == 0, r1.output
    (tmp_path / ".claude" / "CLAUDE.local.md").write_text("# my notes")

    # Second init with --force must overwrite CLAUDE.md but leave the
    # operator-written CLAUDE.local.md alone.
    r2 = runner.invoke(
        init_app,
        [
            "--server-url",
            "http://x",
            "--token",
            "t",
            "--workspace",
            str(tmp_path),
            "--force",
        ],
    )
    assert r2.exit_code == 0, r2.output
    assert "my notes" in (tmp_path / ".claude" / "CLAUDE.local.md").read_text()


def test_init_force_backs_up_existing_claude_md(tmp_path, monkeypatch):
    """Issue #164: --force overwrites CLAUDE.md, but the prior content
    must be preserved as `CLAUDE.md.bak.<timestamp>` so an operator who
    edited it can recover their notes. The backup carries an ISO
    timestamp so re-running --force in the same workspace doesn't
    clobber a prior backup.
    """
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_cfg"))
    api_get = _make_api_get()
    monkeypatch.setattr("cli.commands.init.api_get", api_get, raising=False)
    monkeypatch.setattr("cli.lib.pull.api_get", api_get, raising=False)

    # Seed an existing CLAUDE.md the operator has edited.
    (tmp_path / "CLAUDE.md").write_text("# AI Data Analyst\n\nMy custom edits — must survive reinit.\n")

    r = runner.invoke(
        init_app,
        [
            "--server-url",
            "http://x",
            "--token",
            "t",
            "--workspace",
            str(tmp_path),
            "--force",
        ],
    )
    assert r.exit_code == 0, r.output

    # Backup file: glob since the timestamp is dynamic.
    backups = list(tmp_path.glob("CLAUDE.md.bak.*"))
    assert len(backups) == 1, [p.name for p in backups]
    assert "must survive reinit" in backups[0].read_text()
    # The summary line names the backup so the operator can find it.
    assert "Backed up" in r.output, r.output


def test_init_deletes_bootstrap_token_file(tmp_path, monkeypatch):
    """#580 Finding 1: `agnes init` clears the transient `~/.agnes/token`
    once it has consumed it — the raw PAT must not linger in a plaintext
    file at the default umask after init. The authoritative copy lives in
    ~/.config/agnes/token.json (0o600).
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_cfg"))
    api_get = _make_api_get()
    monkeypatch.setattr("cli.commands.init.api_get", api_get, raising=False)
    monkeypatch.setattr("cli.lib.pull.api_get", api_get, raising=False)

    # Simulate the setup-prompt bootstrap: PAT written to ~/.agnes/token.
    bootstrap_dir = home / ".agnes"
    bootstrap_dir.mkdir()
    token_file = bootstrap_dir / "token"
    token_file.write_text("eyJ-bootstrap-pat\n", encoding="utf-8")

    result = runner.invoke(
        init_app,
        [
            "--server-url",
            "http://x",
            "--token-file",
            str(token_file),
            "--workspace",
            str(tmp_path / "ws"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert not token_file.exists(), "~/.agnes/token should be deleted after init"


def test_init_succeeds_when_bootstrap_token_absent(tmp_path, monkeypatch):
    """The bootstrap-token cleanup is best-effort: init must still succeed
    when ~/.agnes/token was never created (e.g. --token / AGNES_TOKEN path)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_cfg"))
    api_get = _make_api_get()
    monkeypatch.setattr("cli.commands.init.api_get", api_get, raising=False)
    monkeypatch.setattr("cli.lib.pull.api_get", api_get, raising=False)

    result = runner.invoke(
        init_app,
        [
            "--server-url",
            "http://x",
            "--token",
            "inline-pat",
            "--workspace",
            str(tmp_path / "ws"),
        ],
    )
    assert result.exit_code == 0, result.output


def test_init_auto_marks_bootstrap_session_private_when_env_set(tmp_path, monkeypatch):
    """#753: when init runs inside a Claude Code session (CLAUDE_CODE_SESSION_ID
    set), the bootstrap session is auto-marked private so its transcript
    (which may still contain the raw PAT from the setup-prompt heredoc) is
    never uploaded by `agnes push`, even if the user never runs
    `/agnes-private` themselves."""
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_cfg"))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "bootstrap-sid-123")
    api_get = _make_api_get()
    monkeypatch.setattr("cli.commands.init.api_get", api_get, raising=False)
    monkeypatch.setattr("cli.lib.pull.api_get", api_get, raising=False)

    workspace = tmp_path / "ws"
    result = runner.invoke(
        init_app,
        [
            "--server-url",
            "http://test.example.com",
            "--token",
            "test-pat",
            "--workspace",
            str(workspace),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "marked private" in result.output

    from cli.lib.private_list import is_private

    assert is_private(workspace, "bootstrap-sid-123")


def test_init_does_not_mark_private_when_no_session_id(tmp_path, monkeypatch):
    """Outside a Claude Code session (no CLAUDE_CODE_SESSION_ID), there is no
    session to mark — init must not create a private-list entry or fail."""
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_cfg"))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    api_get = _make_api_get()
    monkeypatch.setattr("cli.commands.init.api_get", api_get, raising=False)
    monkeypatch.setattr("cli.lib.pull.api_get", api_get, raising=False)

    workspace = tmp_path / "ws"
    result = runner.invoke(
        init_app,
        [
            "--server-url",
            "http://test.example.com",
            "--token",
            "test-pat",
            "--workspace",
            str(workspace),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "marked private" not in result.output
    assert not (workspace / ".claude" / "agnes-sessions-private.txt").exists()


def test_init_partial_state_friendly_exit(tmp_path, monkeypatch):
    """CLAUDE.md exists with marker but no settings.json -> friendly hint, exit 1."""
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_cfg"))
    workspace = tmp_path
    (workspace / "CLAUDE.md").write_text("# AI Data Analyst\n")
    # Without --force, init should refuse and print a hint
    result = runner.invoke(
        init_app,
        [
            "--server-url",
            "http://x",
            "--token",
            "t",
            "--workspace",
            str(workspace),
        ],
    )
    assert result.exit_code == 1
    assert "Traceback" not in (_clean(result.output) + _clean(result.stderr or ""))


def test_init_auth_failed_on_401(tmp_path, monkeypatch):
    """PAT verify endpoint returns 401 -> auth_failed typed error, exit 1."""
    from unittest.mock import MagicMock

    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_cfg"))

    def _api_get(path, *args, **kwargs):
        resp = MagicMock()
        resp.status_code = 401
        resp.json.return_value = {"detail": "Invalid token"}
        return resp

    monkeypatch.setattr("cli.commands.init.api_get", _api_get, raising=False)

    result = runner.invoke(
        init_app,
        [
            "--server-url",
            "http://x",
            "--token",
            "bad-pat",
            "--workspace",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 1
    output = result.output + (result.stderr or "")
    assert "Traceback" not in output
    # Typed-error envelope should mention the kind or the actionable hint.
    assert ("auth_failed" in output) or ("Token expired" in output) or ("Token format invalid" in output)


def test_init_server_unreachable_on_connect_error(tmp_path, monkeypatch):
    """Network failure during verify -> server_unreachable typed error, exit 1."""
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_cfg"))

    def _api_get(path, *args, **kwargs):
        raise ConnectionError("simulated network failure")

    monkeypatch.setattr("cli.commands.init.api_get", _api_get, raising=False)

    result = runner.invoke(
        init_app,
        [
            "--server-url",
            "http://unreachable.example.com",
            "--token",
            "test-pat",
            "--workspace",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 1
    output = result.output + (result.stderr or "")
    assert "Traceback" not in output
    assert ("server_unreachable" in output) or ("Cannot reach" in output)


def test_init_manifest_unauthorized_when_pull_records_manifest_error(tmp_path, monkeypatch):
    """Manifest stage fails -> manifest_unauthorized typed error, exit 1.

    Reproduces the I1 review finding: `run_pull` records per-stage failures
    into `result.errors` rather than raising. Without the post-pull error
    inspection, init would silently exit 0 with a partially-set-up workspace.
    """
    from unittest.mock import MagicMock
    from cli.lib.pull import PullResult

    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_cfg"))

    # init's verify-PAT call succeeds; welcome-fetch succeeds.
    def _init_api_get(path, *args, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        if path == "/api/welcome":
            resp.json.return_value = {"content": "# Test\n"}
        else:
            resp.json.return_value = []
        return resp

    monkeypatch.setattr("cli.commands.init.api_get", _init_api_get, raising=False)

    # run_pull returns a PullResult carrying a manifest-stage error.
    def _fake_run_pull(server_url, token, workspace, *, dry_run=False):
        result = PullResult()
        result.errors.append({"stage": "manifest", "error": "401 Unauthorized"})
        return result

    monkeypatch.setattr("cli.commands.init.run_pull", _fake_run_pull, raising=False)

    result = runner.invoke(
        init_app,
        [
            "--server-url",
            "http://x",
            "--token",
            "t",
            "--workspace",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 1
    output = result.output + (result.stderr or "")
    assert "Traceback" not in output
    assert ("manifest_unauthorized" in output) or ("Manifest fetch failed" in output)


def test_init_uses_explicit_token_arg_not_stale_disk_token(tmp_path, monkeypatch):
    """Regression for Devin Review finding on init.py:99.

    Repro: a prior `agnes init` left a stale token in
    `~/.config/agnes/token.json`. The new run passes a fresh token via
    `--token`. Pre-fix, step 2's PAT-verify call read the on-disk token
    first and only fell back to the env var — so the explicit `--token`
    arg was silently ignored, the verify ran with the stale token, and
    init failed 401 with a confusing 'token expired' error even though
    the supplied token was valid.

    Fix: a ContextVar-based override (set by `_override_server_env`)
    short-circuits `get_token()` BEFORE the on-disk read.
    """
    import json
    from unittest.mock import MagicMock

    cfg_dir = tmp_path / "_cfg"
    cfg_dir.mkdir()
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(cfg_dir))

    # Seed a stale token on disk — this is what the bug exposed: the verify
    # call would prefer this over the --token arg.
    token_file = cfg_dir / "token.json"
    token_file.write_text(
        json.dumps(
            {
                "access_token": "STALE-DO-NOT-USE",
                "email": "old@example.com",
            }
        ),
        encoding="utf-8",
    )

    captured = {"verify_token": None}

    def _api_get(path, *args, **kwargs):
        # Verify endpoint: snapshot whatever token cli.config.get_token()
        # returns at the moment of the call. If the override is wired
        # correctly, this will be the --token arg, not the stale disk
        # value.
        if path == "/api/catalog/tables":
            from cli.config import get_token

            captured["verify_token"] = get_token()
        resp = MagicMock()
        resp.status_code = 200
        if path == "/api/welcome":
            resp.json.return_value = {"content": "# Test\n"}
        elif path == "/api/sync/manifest":
            resp.json.return_value = {"tables": {}}
        elif path == "/api/memory/bundle":
            resp.json.return_value = {"mandatory": [], "approved": []}
        else:
            resp.json.return_value = []
        return resp

    monkeypatch.setattr("cli.commands.init.api_get", _api_get, raising=False)
    monkeypatch.setattr("cli.lib.pull.api_get", _api_get, raising=False)

    result = runner.invoke(
        init_app,
        [
            "--server-url",
            "http://x",
            "--token",
            "FRESH-PAT-FROM-USER",
            "--workspace",
            str(tmp_path / "ws"),
            "--force",
        ],
    )

    assert captured["verify_token"] == "FRESH-PAT-FROM-USER", (
        "Step 2 verify call must use the explicit --token arg, "
        f"not the stale on-disk token. Got: {captured['verify_token']!r}"
    )
    output = result.output + (result.stderr or "")
    assert "Traceback" not in output


def test_token_override_contextvar_does_not_leak_outside_block():
    """The override must be scoped to the `with` block — leaking it would
    poison subsequent `get_token()` calls (e.g. a long-running daemon
    that runs `agnes init` once and then `agnes pull` later in the same
    process)."""
    from cli.config import _with_token_override, get_token
    import os

    # Sandbox AGNES_CONFIG_DIR so the test's own config dir doesn't muddy
    # the assertion (get_token would fall through to AGNES_TOKEN env or
    # to None depending on host state).
    prior_env = os.environ.pop("AGNES_TOKEN", None)
    try:
        with _with_token_override("INSIDE"):
            assert get_token() == "INSIDE"
        # Outside the block: override cleared, falls through to file/env.
        # Without a config file or AGNES_TOKEN set, returns None.
        assert get_token() != "INSIDE"
    finally:
        if prior_env is not None:
            os.environ["AGNES_TOKEN"] = prior_env


def _make_shortcut_env(tmp_path, monkeypatch, platform="linux"):
    """Set up a fake HOME with workspace, monkeypatch sys.platform."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("sys.platform", platform)
    workspace = tmp_path / "MyWorkspace"
    workspace.mkdir()
    return home, workspace


def test_shortcut_writes_function_to_zshrc_on_linux(tmp_path, monkeypatch):
    """On linux with zsh shell, shortcut appended to ~/.zshrc."""
    home, workspace = _make_shortcut_env(tmp_path, monkeypatch, "linux")
    # Simulate zsh being the default shell
    monkeypatch.setenv("SHELL", "/bin/zsh")
    # Create fake zshrc
    zshrc = home / ".zshrc"
    zshrc.write_text("# existing content\n", encoding="utf-8")

    from cli.lib.shortcut import install_launcher_shortcut

    install_launcher_shortcut(workspace)

    content = zshrc.read_text(encoding="utf-8")
    assert "# >>> agnes launcher: myworkspace <<<" in content
    assert "# <<< agnes launcher: myworkspace >>>" in content
    assert "myworkspace" in content  # word = workspace.name.lower()
    assert "--permission-mode auto" in content


def test_shortcut_writes_function_to_bashrc_on_linux_no_zsh(tmp_path, monkeypatch):
    """On linux without zsh, shortcut appended to ~/.bashrc."""
    home, workspace = _make_shortcut_env(tmp_path, monkeypatch, "linux")
    # No SHELL set → defaults to bashrc
    monkeypatch.delenv("SHELL", raising=False)

    from cli.lib.shortcut import install_launcher_shortcut

    install_launcher_shortcut(workspace)

    bashrc = home / ".bashrc"
    assert bashrc.exists()
    content = bashrc.read_text(encoding="utf-8")
    assert "# >>> agnes launcher: myworkspace <<<" in content
    assert "myworkspace" in content


def test_shortcut_writes_function_to_zshrc_on_macos(tmp_path, monkeypatch):
    """On macOS (darwin), shortcut appended to ~/.zshrc."""
    home, workspace = _make_shortcut_env(tmp_path, monkeypatch, "darwin")
    zshrc = home / ".zshrc"
    zshrc.write_text("", encoding="utf-8")

    from cli.lib.shortcut import install_launcher_shortcut

    install_launcher_shortcut(workspace)

    content = zshrc.read_text(encoding="utf-8")
    assert "# >>> agnes launcher: myworkspace <<<" in content
    assert "myworkspace" in content
    assert "--permission-mode auto" in content


def test_shortcut_routes_via_bin_launcher_when_present(tmp_path, monkeypatch):
    """When bin/<word> exists and is executable, shortcut uses it (not bare `claude`)."""
    home, workspace = _make_shortcut_env(tmp_path, monkeypatch, "linux")
    monkeypatch.delenv("SHELL", raising=False)

    # Create bin/myworkspace launcher
    bin_dir = workspace / "bin"
    bin_dir.mkdir()
    launcher = bin_dir / "myworkspace"
    launcher.write_text('#!/bin/bash\ncd workspace && claude --agent myworkspace "$@"\n')
    launcher.chmod(0o755)

    from cli.lib.shortcut import install_launcher_shortcut

    install_launcher_shortcut(workspace)

    bashrc = home / ".bashrc"
    content = bashrc.read_text(encoding="utf-8")
    assert str(launcher) in content or "bin/myworkspace" in content
    assert "--permission-mode auto" in content


def test_shortcut_falls_back_to_claude_when_no_bin_launcher(tmp_path, monkeypatch):
    """When bin/<word> is absent, shortcut falls back to plain `claude --permission-mode auto`."""
    home, workspace = _make_shortcut_env(tmp_path, monkeypatch, "linux")
    monkeypatch.delenv("SHELL", raising=False)

    from cli.lib.shortcut import install_launcher_shortcut

    install_launcher_shortcut(workspace)

    bashrc = home / ".bashrc"
    content = bashrc.read_text(encoding="utf-8")
    # Should contain `claude --permission-mode auto` (not a bin/ path)
    assert "claude --permission-mode auto" in content


def test_shortcut_idempotent_no_duplicate(tmp_path, monkeypatch):
    """Running install_launcher_shortcut twice must not duplicate the block."""
    home, workspace = _make_shortcut_env(tmp_path, monkeypatch, "linux")
    monkeypatch.delenv("SHELL", raising=False)

    from cli.lib.shortcut import install_launcher_shortcut

    install_launcher_shortcut(workspace)
    install_launcher_shortcut(workspace)  # second call

    bashrc = home / ".bashrc"
    content = bashrc.read_text(encoding="utf-8")
    assert content.count("# >>> agnes launcher: myworkspace <<<") == 1, (
        "Marker appears more than once — shortcut is not idempotent"
    )


def test_shortcut_no_shortcut_flag_writes_nothing(tmp_path, monkeypatch):
    """--no-shortcut: install_launcher_shortcut called with no_shortcut=True writes nothing."""
    home, workspace = _make_shortcut_env(tmp_path, monkeypatch, "linux")
    monkeypatch.delenv("SHELL", raising=False)

    from cli.lib.shortcut import install_launcher_shortcut

    install_launcher_shortcut(workspace, no_shortcut=True)

    bashrc = home / ".bashrc"
    assert not bashrc.exists(), "~/.bashrc must not be created when no_shortcut=True"


def test_shortcut_write_failure_does_not_fail_init(tmp_path, monkeypatch):
    """A write failure in install_launcher_shortcut must not propagate — best-effort."""
    home, workspace = _make_shortcut_env(tmp_path, monkeypatch, "linux")
    monkeypatch.delenv("SHELL", raising=False)

    # Make bashrc a directory (write will fail with IsADirectoryError)
    bashrc = home / ".bashrc"
    bashrc.mkdir()

    from cli.lib.shortcut import install_launcher_shortcut

    # Must not raise
    install_launcher_shortcut(workspace)


def test_init_no_shortcut_flag_accepted(tmp_path, monkeypatch):
    """agnes init --no-shortcut is accepted and exits 0 without writing rc."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_cfg"))
    api_get = _make_api_get()
    monkeypatch.setattr("cli.commands.init.api_get", api_get, raising=False)
    monkeypatch.setattr("cli.lib.pull.api_get", api_get, raising=False)

    result = runner.invoke(
        init_app,
        [
            "--server-url",
            "http://test.example.com",
            "--token",
            "test-pat",
            "--workspace",
            str(tmp_path / "ws"),
            "--no-shortcut",
        ],
    )
    assert result.exit_code == 0, result.output
    # Neither rc file should contain the agnes launcher marker
    zshrc = home / ".zshrc"
    bashrc = home / ".bashrc"
    for rc in (zshrc, bashrc):
        if rc.exists():
            assert "# >>> agnes launcher" not in rc.read_text(), f"{rc} contains launcher marker despite --no-shortcut"


def test_init_writes_shortcut_and_reports_it(tmp_path, monkeypatch):
    """Agnes init (without --no-shortcut) creates shortcut and reports it in summary."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_cfg"))
    # Use a distinctive workspace name so we can assert on the word
    ws = tmp_path / "TestBrand"
    api_get = _make_api_get()
    monkeypatch.setattr("cli.commands.init.api_get", api_get, raising=False)
    monkeypatch.setattr("cli.lib.pull.api_get", api_get, raising=False)

    result = runner.invoke(
        init_app,
        [
            "--server-url",
            "http://test.example.com",
            "--token",
            "test-pat",
            "--workspace",
            str(ws),
        ],
    )
    assert result.exit_code == 0, result.output
    # Summary mentions shortcut
    assert "Shortcut" in result.output or "testbrand" in result.output


def test_shortcut_second_workspace_gets_own_block(tmp_path, monkeypatch):
    """Per-workspace marker: a second, differently-named workspace adds its own
    block instead of being skipped by the first workspace's marker."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.delenv("SHELL", raising=False)
    ws_a = tmp_path / "Alpha"
    ws_a.mkdir()
    ws_b = tmp_path / "Beta"
    ws_b.mkdir()

    from cli.lib.shortcut import install_launcher_shortcut

    install_launcher_shortcut(ws_a)
    install_launcher_shortcut(ws_b)

    content = (home / ".bashrc").read_text(encoding="utf-8")
    assert "# >>> agnes launcher: alpha <<<" in content
    assert "# >>> agnes launcher: beta <<<" in content
    assert content.count("# >>> agnes launcher:") == 2


def test_shortcut_warns_on_foreign_function(tmp_path, monkeypatch, capsys):
    """A pre-existing same-named function without our marker is preserved; we
    append our marked block (last definition wins) and warn the user."""
    home, workspace = _make_shortcut_env(tmp_path, monkeypatch, "linux")
    monkeypatch.delenv("SHELL", raising=False)
    bashrc = home / ".bashrc"
    # word for MyWorkspace is "myworkspace"
    bashrc.write_text("function myworkspace { cd ~/old && claude; }\n", encoding="utf-8")

    from cli.lib.shortcut import install_launcher_shortcut

    install_launcher_shortcut(workspace)

    content = bashrc.read_text(encoding="utf-8")
    assert "# >>> agnes launcher: myworkspace <<<" in content
    assert content.count("function myworkspace") == 2  # old one preserved
    captured = capsys.readouterr()
    assert "myworkspace" in captured.err
    assert "delete the old line" in captured.err


def test_shortcut_windows_writes_both_powershell_profiles(tmp_path, monkeypatch):
    """On Windows the function lands in BOTH the 5.x (WindowsPowerShell) and
    7+ (PowerShell) profile locations, so it loads in either edition."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("sys.platform", "win32")
    workspace = tmp_path / "MyWorkspace"
    workspace.mkdir()

    from cli.lib.shortcut import install_launcher_shortcut

    install_launcher_shortcut(workspace)

    ps5 = home / "Documents" / "WindowsPowerShell" / "Microsoft.PowerShell_profile.ps1"
    ps7 = home / "Documents" / "PowerShell" / "Microsoft.PowerShell_profile.ps1"
    for prof in (ps5, ps7):
        assert prof.exists(), f"{prof} was not written"
        content = prof.read_text(encoding="utf-8")
        assert "# >>> agnes launcher: myworkspace <<<" in content
        assert "--permission-mode auto" in content


def test_shortcut_sanitizes_workspace_name_with_special_chars(tmp_path, monkeypatch):
    """A workspace folder with spaces/dots yields a valid alphanumeric function
    name — never raw special chars that would break the rc file."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.delenv("SHELL", raising=False)
    workspace = tmp_path / "My Team A.I."
    workspace.mkdir()

    from cli.lib.shortcut import install_launcher_shortcut

    install_launcher_shortcut(workspace)

    content = (home / ".bashrc").read_text(encoding="utf-8")
    assert "function myteamai {" in content
    assert "# >>> agnes launcher: myteamai <<<" in content
    assert "function My Team" not in content  # no raw spaces leaked


def test_shortcut_skips_when_name_has_no_alphanumerics(tmp_path, monkeypatch, capsys):
    """A workspace name with zero alphanumerics can't form a function name —
    skip and warn instead of writing broken shell."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.delenv("SHELL", raising=False)
    workspace = tmp_path / "___"
    workspace.mkdir()

    from cli.lib.shortcut import install_launcher_shortcut

    install_launcher_shortcut(workspace)

    assert not (home / ".bashrc").exists()
    captured = capsys.readouterr()
    assert "skipping shortcut" in captured.err


def test_launcher_word_avoids_shell_builtin_collision():
    """A workspace named 'Test' would produce 'test' which shadows the POSIX
    built-in; the guard appends 'ai' so the function name is 'testai'."""
    from cli.lib import shortcut as sc

    assert sc._launcher_word("Test") == "testai"
    assert sc._launcher_word("CD") == "cdai"
    # Non-built-in names are unaffected.
    assert sc._launcher_word("MyWorkspace") == "myworkspace"
    assert sc._launcher_word("Agnes") == "agnes"
