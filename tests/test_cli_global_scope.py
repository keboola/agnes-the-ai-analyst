"""`agnes global` command group (spec §6). All state isolated: AGNES_CONFIG_DIR
+ patched user paths + recorded subprocess.run — no real `claude`, no network."""

import json
import subprocess

import pytest
import yaml
from typer.testing import CliRunner

import cli.commands.global_scope as gs_module


class _Recorder:
    def __init__(self):
        self.calls: list[list[str]] = []
        self.scripts: list[tuple[tuple[str, ...], subprocess.CompletedProcess]] = []

    def run(self, cmd, *args, **kwargs):
        self.calls.append(list(cmd))
        for prefix, scripted in sorted(self.scripts, key=lambda s: -len(s[0])):
            if tuple(cmd[: len(prefix)]) == prefix:
                return scripted
        return subprocess.CompletedProcess(args=list(cmd), returncode=0, stdout="", stderr="")


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "agnes-cfg"
    cfg_dir.mkdir()
    (cfg_dir / "token.json").write_text(json.dumps({"access_token": "pat", "email": "t@example.com"}), encoding="utf-8")
    (cfg_dir / "config.yaml").write_text(
        "server: http://localhost:9\nworkspace_root: " + str(tmp_path / "ws") + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(cfg_dir))

    claude_md = tmp_path / "user-claude" / "CLAUDE.md"
    settings = tmp_path / "user-claude" / "settings.json"
    monkeypatch.setattr("cli.lib.user_scope.user_claude_md_path", lambda: claude_md)
    monkeypatch.setattr("cli.lib.user_scope.user_settings_path", lambda: settings)
    monkeypatch.setattr(gs_module, "user_claude_md_path", lambda: claude_md)

    clone = tmp_path / "marketplace"
    (clone / ".git").mkdir(parents=True)
    (clone / ".claude-plugin").mkdir(parents=True)
    (clone / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps({"name": "agnes", "plugins": [{"name": "alpha", "source": "./plugins/alpha", "version": "1.0.0"}]}),
        encoding="utf-8",
    )
    import cli.commands.refresh_marketplace as rm_module

    monkeypatch.setattr(rm_module, "CLONE_DIR", clone)

    rec = _Recorder()
    monkeypatch.setattr(rm_module.subprocess, "run", rec.run)
    monkeypatch.setattr(gs_module.subprocess, "run", rec.run)
    monkeypatch.setattr(rm_module.shutil, "which", lambda n: "claude" if n == "claude" else None)
    monkeypatch.setattr(gs_module.shutil, "which", lambda n: {"claude": "claude", "agnes": "/fake/bin/agnes"}.get(n))
    rec.scripts.append(
        (
            ("claude", "plugin", "list", "--json"),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="[]", stderr=""),
        )
    )
    rec.scripts.append(
        (
            ("claude", "mcp", "get"),
            subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="not found"),
        )
    )
    monkeypatch.setattr(gs_module, "_verify_credentials", lambda: True)
    return {"rec": rec, "claude_md": claude_md, "settings": settings, "cfg_dir": cfg_dir}


def test_enable_converges_all_artifacts(env):
    result = CliRunner().invoke(gs_module.global_app, ["enable"])
    assert result.exit_code == 0, result.output
    rec = env["rec"]
    assert ["claude", "plugin", "install", "alpha@agnes", "--scope", "user"] in rec.calls
    mcp_adds = [c for c in rec.calls if c[:3] == ["claude", "mcp", "add"]]
    assert mcp_adds and "--scope" in mcp_adds[0] and "user" in mcp_adds[0] and mcp_adds[0][-1] == "mcp"
    assert env["claude_md"].exists()
    cfg = json.loads(env["settings"].read_text(encoding="utf-8"))
    assert cfg["hooks"]["SessionStart"], "hook installed by default (spec §13.2)"
    conf = yaml.safe_load((env["cfg_dir"] / "config.yaml").read_text(encoding="utf-8"))
    assert conf["global_scope"] is True and conf["global_hook"] is True


def test_enable_no_hook(env):
    result = CliRunner().invoke(gs_module.global_app, ["enable", "--no-hook"])
    assert result.exit_code == 0, result.output
    assert not env["settings"].exists() or "hooks" not in json.loads(env["settings"].read_text(encoding="utf-8"))
    conf = yaml.safe_load((env["cfg_dir"] / "config.yaml").read_text(encoding="utf-8"))
    assert conf["global_hook"] is False


def test_enable_twice_idempotent(env):
    CliRunner().invoke(gs_module.global_app, ["enable"])
    md_before = env["claude_md"].read_text(encoding="utf-8")
    settings_before = env["settings"].read_text(encoding="utf-8")
    result = CliRunner().invoke(gs_module.global_app, ["enable"])
    assert result.exit_code == 0
    assert env["claude_md"].read_text(encoding="utf-8") == md_before
    assert env["settings"].read_text(encoding="utf-8") == settings_before


def test_enable_json_report(env):
    result = CliRunner().invoke(gs_module.global_app, ["enable", "--json"])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.output)
    stages = {row["stage"] for row in doc["report"]}
    assert {"plugins", "mcp", "rails", "hook", "flag"} <= stages


def test_disable_reverts_exactly(env):
    CliRunner().invoke(gs_module.global_app, ["enable"])
    env["rec"].scripts.insert(
        0,
        (
            ("claude", "mcp", "get"),
            subprocess.CompletedProcess(
                args=[], returncode=0, stdout="agnes:\n  Command: /fake/bin/agnes\n  Args: mcp\n", stderr=""
            ),
        ),
    )
    env["rec"].scripts.insert(
        0,
        (
            ("claude", "plugin", "list", "--json"),
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout='[{"id": "alpha@agnes", "version": "1.0.0", "projectPath": null, "scope": "user"}]',
                stderr="",
            ),
        ),
    )
    result = CliRunner().invoke(gs_module.global_app, ["disable"])
    assert result.exit_code == 0, result.output
    rec = env["rec"]
    assert ["claude", "plugin", "uninstall", "alpha@agnes", "--scope", "user"] in rec.calls
    assert any(c[:3] == ["claude", "mcp", "remove"] and "user" in c for c in rec.calls)
    assert not env["claude_md"].exists() or "agnes-global" not in env["claude_md"].read_text(encoding="utf-8")
    cfg = json.loads(env["settings"].read_text(encoding="utf-8"))
    assert not any(cfg.get("hooks", {}).get("SessionStart", []))
    conf = yaml.safe_load((env["cfg_dir"] / "config.yaml").read_text(encoding="utf-8"))
    assert conf["global_scope"] is False


def test_disable_leaves_foreign_mcp_entry(env):
    CliRunner().invoke(gs_module.global_app, ["enable"])
    env["rec"].calls.clear()
    env["rec"].scripts.insert(
        0,
        (
            ("claude", "mcp", "get"),
            subprocess.CompletedProcess(
                args=[], returncode=0, stdout="agnes:\n  Command: /usr/bin/somethingelse\n  Args: serve\n", stderr=""
            ),
        ),
    )
    result = CliRunner().invoke(gs_module.global_app, ["disable"])
    assert result.exit_code == 0
    assert not any(c[:3] == ["claude", "mcp", "remove"] for c in env["rec"].calls)


def test_status_reports_each_artifact(env):
    CliRunner().invoke(gs_module.global_app, ["enable"])
    env["rec"].scripts.insert(
        0,
        (
            ("claude", "mcp", "get"),
            subprocess.CompletedProcess(
                args=[], returncode=0, stdout="agnes:\n  Command: /fake/bin/agnes\n  Args: mcp\n", stderr=""
            ),
        ),
    )
    result = CliRunner().invoke(gs_module.global_app, ["status", "--json"])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.output)
    rows = {row["artifact"]: row["state"] for row in doc["artifacts"]}
    assert rows["rails"] == "ok"
    assert rows["hook"] == "ok"
    assert rows["flag"] == "ok"
    assert rows["mcp"] == "ok"
    assert "plugins" in rows
