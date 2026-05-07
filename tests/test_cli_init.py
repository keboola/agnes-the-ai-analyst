"""Tests for `agnes init` orchestrator command."""

from typer.testing import CliRunner

# CI-safety: Typer/rich emits ANSI escapes in --help output. Strip before asserts.
_ANSI_RE = __import__("re").compile(r"\x1b\[[0-9;]*m")
def _clean(s: str) -> str:
    return _ANSI_RE.sub("", s)

from cli.commands.init import init_app

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

    result = runner.invoke(init_app, [
        "--server-url", "http://test.example.com",
        "--token", "test-pat",
        "--workspace", str(tmp_path),
    ])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "CLAUDE.md").exists()
    assert "agnes pull" in (tmp_path / "CLAUDE.md").read_text()
    assert (tmp_path / ".claude" / "settings.json").exists()
    assert (tmp_path / ".claude" / "CLAUDE.local.md").exists()
    assert (tmp_path / "AGNES_WORKSPACE.md").exists()
    # run_pull always creates the analytics.duckdb file (load-bearing).
    assert (tmp_path / "user" / "duckdb" / "analytics.duckdb").exists()


def test_init_no_dead_dirs_zero_grants(tmp_path, monkeypatch):
    """Zero grants -> no .claude/rules, no server/parquet, no user/sessions."""
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_cfg"))
    api_get = _make_api_get()
    monkeypatch.setattr("cli.commands.init.api_get", api_get, raising=False)
    monkeypatch.setattr("cli.lib.pull.api_get", api_get, raising=False)

    runner.invoke(init_app, [
        "--server-url", "http://x",
        "--token", "t",
        "--workspace", str(tmp_path),
    ])
    for forbidden in [
        "data/parquet", "data/duckdb", "data/metadata",
        "user/artifacts", "user/sessions",
        "server/parquet", ".claude/rules",
    ]:
        assert not (tmp_path / forbidden).exists(), f"forbidden created: {forbidden}"


def test_init_force_preserves_local_md(tmp_path, monkeypatch):
    """--force regenerates CLAUDE.md but never touches CLAUDE.local.md."""
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_cfg"))
    api_get = _make_api_get()
    monkeypatch.setattr("cli.commands.init.api_get", api_get, raising=False)
    monkeypatch.setattr("cli.lib.pull.api_get", api_get, raising=False)

    # First init seeds the workspace + writes the default CLAUDE.local.md stub.
    r1 = runner.invoke(init_app, [
        "--server-url", "http://x",
        "--token", "t",
        "--workspace", str(tmp_path),
    ])
    assert r1.exit_code == 0, r1.output
    (tmp_path / ".claude" / "CLAUDE.local.md").write_text("# my notes")

    # Second init with --force must overwrite CLAUDE.md but leave the
    # operator-written CLAUDE.local.md alone.
    r2 = runner.invoke(init_app, [
        "--server-url", "http://x",
        "--token", "t",
        "--workspace", str(tmp_path),
        "--force",
    ])
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
    (tmp_path / "CLAUDE.md").write_text(
        "# AI Data Analyst\n\nMy custom edits — must survive reinit.\n"
    )

    r = runner.invoke(init_app, [
        "--server-url", "http://x",
        "--token", "t",
        "--workspace", str(tmp_path),
        "--force",
    ])
    assert r.exit_code == 0, r.output

    # Backup file: glob since the timestamp is dynamic.
    backups = list(tmp_path.glob("CLAUDE.md.bak.*"))
    assert len(backups) == 1, [p.name for p in backups]
    assert "must survive reinit" in backups[0].read_text()
    # The summary line names the backup so the operator can find it.
    assert "Backed up" in r.output, r.output


def test_init_partial_state_friendly_exit(tmp_path, monkeypatch):
    """CLAUDE.md exists with marker but no settings.json -> friendly hint, exit 1."""
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_cfg"))
    workspace = tmp_path
    (workspace / "CLAUDE.md").write_text("# AI Data Analyst\n")
    # Without --force, init should refuse and print a hint
    result = runner.invoke(init_app, [
        "--server-url", "http://x",
        "--token", "t",
        "--workspace", str(workspace),
    ])
    assert result.exit_code == 1
    assert "Traceback" not in (_clean(result.output) + _clean(result.stderr or ''))


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

    result = runner.invoke(init_app, [
        "--server-url", "http://x",
        "--token", "bad-pat",
        "--workspace", str(tmp_path),
    ])
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

    result = runner.invoke(init_app, [
        "--server-url", "http://unreachable.example.com",
        "--token", "test-pat",
        "--workspace", str(tmp_path),
    ])
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

    result = runner.invoke(init_app, [
        "--server-url", "http://x",
        "--token", "t",
        "--workspace", str(tmp_path),
    ])
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
    token_file.write_text(json.dumps({
        "access_token": "STALE-DO-NOT-USE",
        "email": "old@example.com",
    }), encoding="utf-8")

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

    result = runner.invoke(init_app, [
        "--server-url", "http://x",
        "--token", "FRESH-PAT-FROM-USER",
        "--workspace", str(tmp_path / "ws"),
        "--force",
    ])

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
