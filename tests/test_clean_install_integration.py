"""End-to-end clean-install integration tests for `agnes init`."""

import json
import os
import subprocess
import sys
from pathlib import Path


AGNES = [sys.executable, "-m", "cli.main"]


def _isolated_env(tmp_path: Path) -> dict:
    """Env with `AGNES_CONFIG_DIR` pointing into tmp_path.

    `cli.config.get_token()` reads `~/.config/agnes/token.json` first and
    only falls back to `AGNES_TOKEN`. Without this isolation a stale token
    on the developer's machine would override the test_pat passed via
    `--token`. Same shape as Task 20's `zero_grants_workspace` fixture.
    """
    env = os.environ.copy()
    config_dir = tmp_path / "agnes-config"
    config_dir.mkdir(parents=True, exist_ok=True)
    env["AGNES_CONFIG_DIR"] = str(config_dir)
    # `agnes init` installs a launcher shortcut into the shell rc under
    # $HOME — redirect it into tmp so the subprocess cannot append marker
    # blocks to the developer's real ~/.zshrc (guard in tests/conftest.py).
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir(parents=True, exist_ok=True)
    env["HOME"] = str(fake_home)
    return env


def assert_no_dead_dirs(workspace: Path):
    """Lazy-mkdir contract: forbidden dirs absent; conditionally-empty dirs only when populated."""
    forbidden_unconditional = ["data/parquet", "data/duckdb", "data/metadata",
                               "user/artifacts", ".agnes"]
    for d in forbidden_unconditional:
        assert not (workspace / d).exists(), f"forbidden dir created: {d}"
    for d in [".claude/rules", "server/parquet", "user/sessions", "user/snapshots"]:
        path = workspace / d
        if path.exists():
            assert any(path.iterdir()), f"{d} exists but is empty"


def test_clean_install_minimal_grants(fastapi_test_server, tmp_path, test_pat):
    """`agnes init` with grants → CLAUDE.md, AGNES_WORKSPACE.md, hooks, parquets, DuckDB."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    result = subprocess.run(AGNES + [
        "init",
        "--server-url", fastapi_test_server.url,
        "--token", test_pat,
        "--workspace", str(workspace),
    ], env=_isolated_env(tmp_path), capture_output=True, text=True)
    assert result.returncode == 0, f"init failed: {result.stderr}"

    # Required files
    for must in ["CLAUDE.md", "AGNES_WORKSPACE.md",
                 ".claude/settings.json", ".claude/CLAUDE.local.md",
                 "user/duckdb/analytics.duckdb"]:
        assert (workspace / must).exists(), f"missing: {must}"

    # Grants → 2 parquets exist (local + materialized; remote is skipped per query_mode)
    parquets = list((workspace / "server" / "parquet").glob("*.parquet"))
    assert len(parquets) >= 1, f"expected >=1 parquet, got {len(parquets)}: {parquets}"

    # No dead dirs
    assert_no_dead_dirs(workspace)

    # Hooks installed — SessionStart now runs the single detached `agnes update`
    # convergence (which pulls data internally) in place of a direct `agnes pull`.
    settings = json.loads((workspace / ".claude" / "settings.json").read_text())
    assert any("agnes update" in h["hooks"][0]["command"]
               for h in settings.get("hooks", {}).get("SessionStart", []))
    assert any("agnes push" in h["hooks"][0]["command"]
               for h in settings.get("hooks", {}).get("SessionEnd", []))

    # CLAUDE.md was fetched from /api/welcome (not from local template)
    claude_md = (workspace / "CLAUDE.md").read_text()
    assert "agnes pull" in claude_md
    assert "da sync" not in claude_md  # post-rewrite content

    # AGNES_WORKSPACE.md content
    workspace_md = (workspace / "AGNES_WORKSPACE.md").read_text()
    assert test_pat not in workspace_md, "PAT must not leak into AGNES_WORKSPACE.md"
    for placeholder in ["{created_at}", "{server_url}", "{workspace_path}"]:
        assert placeholder not in workspace_md, f"placeholder leaked: {placeholder}"
    assert fastapi_test_server.url in workspace_md
    assert str(workspace) in workspace_md
    assert "agnes pull" in workspace_md  # cheat sheet uses new verb


def test_clean_install_zero_grants(fastapi_test_server, tmp_path, test_pat_no_grants):
    """Zero grants → minimal workspace; no parquets, no rules, no dead dirs."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    result = subprocess.run(AGNES + [
        "init",
        "--server-url", fastapi_test_server.url,
        "--token", test_pat_no_grants,
        "--workspace", str(workspace),
    ], env=_isolated_env(tmp_path), capture_output=True, text=True)
    assert result.returncode == 0, f"init failed: {result.stderr}"

    must_exist = {"CLAUDE.md", "AGNES_WORKSPACE.md",
                  ".claude/settings.json", ".claude/CLAUDE.local.md",
                  "user/duckdb/analytics.duckdb"}
    must_not_exist = {".claude/rules", "server/parquet", "data/parquet",
                      "data/duckdb", "data/metadata", "user/artifacts",
                      "user/sessions", "user/snapshots", ".agnes"}
    for p in must_exist:
        assert (workspace / p).exists(), f"missing: {p}"
    for p in must_not_exist:
        assert not (workspace / p).exists(), f"unexpected: {p}"
    assert_no_dead_dirs(workspace)


def test_init_force_preserves_local_md(fastapi_test_server, tmp_path, test_pat):
    """`agnes init --force` regenerates CLAUDE.md but never touches CLAUDE.local.md."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    env = _isolated_env(tmp_path)
    result1 = subprocess.run(AGNES + ["init",
                                      "--server-url", fastapi_test_server.url,
                                      "--token", test_pat, "--workspace", str(workspace)],
                             env=env, capture_output=True, text=True)
    assert result1.returncode == 0, f"first init failed: {result1.stderr}"
    (workspace / ".claude" / "CLAUDE.local.md").write_text("# my private notes\n")

    result2 = subprocess.run(AGNES + ["init",
                                      "--server-url", fastapi_test_server.url,
                                      "--token", test_pat, "--workspace", str(workspace),
                                      "--force"],
                             env=env, capture_output=True, text=True)
    assert result2.returncode == 0, f"force init failed: {result2.stderr}"
    assert "my private notes" in (workspace / ".claude" / "CLAUDE.local.md").read_text()


def test_readers_in_pre_init_dir(tmp_path):
    """Reader commands in a folder that never had `agnes init`. Friendly hints, no tracebacks."""
    env = _isolated_env(tmp_path)
    for cmd in [AGNES + ["query", "SELECT 1"],
                AGNES + ["snapshot", "create", "x", "--as", "y", "--estimate"],
                AGNES + ["explore", "x"],
                AGNES + ["snapshot", "list"]]:
        result = subprocess.run(cmd, cwd=tmp_path, env=env,
                                capture_output=True, text=True, timeout=15)
        assert "Traceback" not in result.stderr, f"{cmd} threw: {result.stderr}"
