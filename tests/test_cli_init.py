"""Tests for `agnes init` orchestrator command."""

from typer.testing import CliRunner

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
    assert "--server-url" in result.output
    assert "--token" in result.output
    assert "--force" in result.output
    assert "--workspace" in result.output


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
    assert "Traceback" not in (result.output + (result.stderr or ""))
