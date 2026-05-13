"""Tests for the `agnes init` override flow + supporting helpers.

Covers:
  * `cli.lib.override.is_override_workspace` — sentinel parse semantics
  * `cli.lib.initial_workspace.probe_status` — 404 fall-through + happy path
  * `cli.lib.initial_workspace.extract_zip_to_workspace` — safe extraction
  * `cli.lib.initial_workspace.prompt_force_confirmation` — YES strictness
  * `cli.lib.hooks.install_claude_hooks` — no-op on override workspace
  * `cli.lib.hooks.maybe_refresh_claude_hooks` — no-op on override workspace
  * `cli.lib.commands.install_claude_commands` — no-op on override workspace
  * `agnes init` end-to-end with mocked endpoints (default + override)
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _clean(s: str) -> str:
    return _ANSI_RE.sub("", s)


# ===========================================================================
# Layer 1: cli/lib/override.py — sentinel parse semantics
# ===========================================================================


def _write_sentinel(workspace: Path, contents: str) -> None:
    sentinel = workspace / ".claude" / "init-complete"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text(contents, encoding="utf-8")


def test_override_no_sentinel(tmp_path):
    from cli.lib.override import is_override_workspace
    assert is_override_workspace(tmp_path) is False


def test_override_sentinel_without_override_key(tmp_path):
    from cli.lib.override import is_override_workspace
    _write_sentinel(tmp_path, "completed_at: 2026-05-13T00:00:00Z\nagnes_version: 0.54.1\n")
    assert is_override_workspace(tmp_path) is False


def test_override_sentinel_with_override_true(tmp_path):
    from cli.lib.override import is_override_workspace
    _write_sentinel(tmp_path, "override: true\n")
    assert is_override_workspace(tmp_path) is True


def test_override_sentinel_with_override_false(tmp_path):
    from cli.lib.override import is_override_workspace
    _write_sentinel(tmp_path, "override: false\n")
    assert is_override_workspace(tmp_path) is False


def test_override_sentinel_case_insensitive(tmp_path):
    from cli.lib.override import is_override_workspace
    _write_sentinel(tmp_path, "override: TRUE\n")
    assert is_override_workspace(tmp_path) is True


def test_read_override_metadata_returns_dict(tmp_path):
    from cli.lib.override import read_override_metadata
    _write_sentinel(
        tmp_path,
        "completed_at: 2026-05-13T00:00:00Z\n"
        "override: true\n"
        "template_source: https://example.com/repo\n"
        "template_sha: abc123\n",
    )
    data = read_override_metadata(tmp_path)
    assert data is not None
    assert data["override"] == "true"
    assert data["template_source"] == "https://example.com/repo"
    assert data["template_sha"] == "abc123"


# ===========================================================================
# Layer 2: probe_status — 404 fall-through + happy paths
# ===========================================================================


def _mock_resp(status_code: int, body=None, content: bytes = b""):
    resp = MagicMock()
    resp.status_code = status_code
    if body is not None:
        resp.json.return_value = body
    resp.content = content
    return resp


def test_probe_status_404_returns_none(monkeypatch):
    """Old server doesn't know the endpoint → silent fall-through."""
    from cli.lib import initial_workspace

    monkeypatch.setattr(initial_workspace, "api_get", lambda *a, **k: _mock_resp(404))
    monkeypatch.setenv("AGNES_TOKEN", "t")
    result = initial_workspace.probe_status("http://x", "t")
    assert result is None


def test_probe_status_configured_false_returns_StatusInfo(monkeypatch):
    from cli.lib import initial_workspace

    monkeypatch.setattr(
        initial_workspace,
        "api_get",
        lambda *a, **k: _mock_resp(200, body={"configured": False}),
    )
    monkeypatch.setenv("AGNES_TOKEN", "t")
    result = initial_workspace.probe_status("http://x", "t")
    assert result is not None
    assert result.configured is False


def test_probe_status_configured_true_full_metadata(monkeypatch):
    from cli.lib import initial_workspace

    body = {
        "configured": True,
        "synced": True,
        "template_source": "https://github.com/example/template",
        "template_sha": "1a2b3c4d",
        "synced_at": "2026-05-13T10:00:00Z",
        "files": ["CLAUDE.md", ".claude/settings.json"],
    }
    monkeypatch.setattr(
        initial_workspace, "api_get", lambda *a, **k: _mock_resp(200, body=body)
    )
    monkeypatch.setenv("AGNES_TOKEN", "t")
    result = initial_workspace.probe_status("http://x", "t")
    assert result.configured is True
    assert result.synced is True
    assert result.template_sha == "1a2b3c4d"
    assert "CLAUDE.md" in result.files


# ===========================================================================
# Layer 3: extract_zip_to_workspace — safe extraction
# ===========================================================================


def _make_zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_extract_zip_creates_files(tmp_path):
    from cli.lib.initial_workspace import extract_zip_to_workspace

    data = _make_zip({
        "CLAUDE.md": b"# Custom\n",
        ".claude/settings.json": b'{"model": "sonnet"}',
    })
    result = extract_zip_to_workspace(data, tmp_path)
    assert sorted(result.created) == [".claude/settings.json", "CLAUDE.md"]
    assert result.overwritten == []
    assert (tmp_path / "CLAUDE.md").read_text() == "# Custom\n"


def test_extract_zip_distinguishes_overwrite_vs_create(tmp_path):
    from cli.lib.initial_workspace import extract_zip_to_workspace

    (tmp_path / "CLAUDE.md").write_text("old content\n")
    data = _make_zip({
        "CLAUDE.md": b"# New\n",
        "docs/handbook.md": b"# Handbook\n",
    })
    result = extract_zip_to_workspace(data, tmp_path)
    assert result.overwritten == ["CLAUDE.md"]
    assert result.created == ["docs/handbook.md"]
    assert (tmp_path / "CLAUDE.md").read_text() == "# New\n"


def test_extract_zip_rejects_dotdot_entry(tmp_path):
    import typer
    from cli.lib.initial_workspace import extract_zip_to_workspace

    data = _make_zip({"../escape.txt": b"naughty"})
    with pytest.raises(typer.Exit):
        extract_zip_to_workspace(data, tmp_path)
    # Critical: nothing got written outside the workspace
    assert not (tmp_path.parent / "escape.txt").exists()


def test_extract_zip_rejects_absolute_entry(tmp_path):
    import typer
    from cli.lib.initial_workspace import extract_zip_to_workspace

    data = _make_zip({"/etc/passwd": b"naughty"})
    with pytest.raises(typer.Exit):
        extract_zip_to_workspace(data, tmp_path)


# ===========================================================================
# Layer 4: prompt_force_confirmation — YES strictness
# ===========================================================================


def test_confirmation_yes_returns_true(monkeypatch):
    import typer
    from cli.lib.initial_workspace import prompt_force_confirmation

    monkeypatch.setattr(typer, "prompt", lambda *a, **k: "YES")
    assert prompt_force_confirmation(
        Path("/tmp/ws"), ["CLAUDE.md"], ["docs/x.md"]
    ) is True


def test_confirmation_lowercase_yes_returns_false(monkeypatch):
    import typer
    from cli.lib.initial_workspace import prompt_force_confirmation

    monkeypatch.setattr(typer, "prompt", lambda *a, **k: "yes")
    assert prompt_force_confirmation(Path("/tmp/ws"), [], []) is False


def test_confirmation_no_returns_false(monkeypatch):
    import typer
    from cli.lib.initial_workspace import prompt_force_confirmation

    monkeypatch.setattr(typer, "prompt", lambda *a, **k: "no")
    assert prompt_force_confirmation(Path("/tmp/ws"), [], []) is False


def test_confirmation_empty_returns_false(monkeypatch):
    import typer
    from cli.lib.initial_workspace import prompt_force_confirmation

    monkeypatch.setattr(typer, "prompt", lambda *a, **k: "")
    assert prompt_force_confirmation(Path("/tmp/ws"), [], []) is False


def test_confirmation_whitespace_yes_returns_true(monkeypatch):
    """`  YES  ` with whitespace should still pass (stripped)."""
    import typer
    from cli.lib.initial_workspace import prompt_force_confirmation

    monkeypatch.setattr(typer, "prompt", lambda *a, **k: "  YES  ")
    assert prompt_force_confirmation(Path("/tmp/ws"), [], []) is True


# ===========================================================================
# Layer 5: hook + command guards on override workspace
# ===========================================================================


def test_install_claude_hooks_noop_on_override(tmp_path):
    """install_claude_hooks short-circuits when override sentinel present."""
    from cli.lib.hooks import install_claude_hooks

    _write_sentinel(tmp_path, "override: true\n")
    install_claude_hooks(tmp_path)
    # Should NOT have created settings.json or modified anything in .claude/
    settings = tmp_path / ".claude" / "settings.json"
    assert not settings.exists()


def test_install_claude_hooks_runs_on_default_workspace(tmp_path):
    """No sentinel = default workspace, hooks install normally."""
    from cli.lib.hooks import install_claude_hooks

    install_claude_hooks(tmp_path)
    settings = tmp_path / ".claude" / "settings.json"
    assert settings.exists()
    cfg = json.loads(settings.read_text())
    assert "hooks" in cfg


def test_maybe_refresh_claude_hooks_noop_on_override(tmp_path):
    """maybe_refresh_claude_hooks returns False on override workspace
    even when the workspace LOOKS like an Agnes workspace (has agnes hooks)."""
    from cli.lib.hooks import maybe_refresh_claude_hooks

    # First write some agnes-looking hooks so workspace_has_agnes_hooks True
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command", "command": "agnes pull --quiet"}]}
            ]
        }
    }))
    # Override sentinel — should now short-circuit the refresh
    _write_sentinel(tmp_path, "override: true\n")

    assert maybe_refresh_claude_hooks(tmp_path) is False
    # Verify settings.json wasn't rewritten with the Agnes default hooks
    cfg = json.loads(settings_path.read_text())
    cmds = [
        h["command"]
        for entry in cfg["hooks"]["SessionStart"]
        for h in entry["hooks"]
    ]
    # Original single command intact — no capture-session / refresh-marketplace added
    assert cmds == ["agnes pull --quiet"]


def test_install_claude_commands_noop_on_override(tmp_path):
    from cli.lib.commands import install_claude_commands

    _write_sentinel(tmp_path, "override: true\n")
    install_claude_commands(tmp_path)
    commands_dir = tmp_path / ".claude" / "commands"
    assert not commands_dir.exists() or list(commands_dir.iterdir()) == []


# ===========================================================================
# Layer 6: agnes init end-to-end (mocked endpoints)
# ===========================================================================


from cli.commands.init import init_app  # noqa: E402

runner = CliRunner()


def _build_api_get(initial_workspace_status: dict | None, zip_bytes: bytes = b""):
    """Build a stub api_get with configurable /api/initial-workspace response.

    Args:
        initial_workspace_status: None → 404 (old server simulation).
                                  Dict → 200 with that body.
        zip_bytes: bytes for /api/initial-workspace.zip when needed.
    """
    def _api_get(path, *args, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        resp.content = b""
        if path == "/api/catalog/tables":
            resp.json.return_value = []
        elif path == "/api/welcome":
            resp.json.return_value = {
                "content": "# Default CLAUDE.md\n",
            }
        elif path == "/api/sync/manifest":
            resp.json.return_value = {"tables": {}}
        elif path == "/api/memory/bundle":
            resp.json.return_value = {"mandatory": [], "approved": []}
        elif path == "/api/initial-workspace":
            if initial_workspace_status is None:
                resp.status_code = 404
            else:
                resp.json.return_value = initial_workspace_status
        elif path == "/api/initial-workspace.zip":
            resp.content = zip_bytes
            resp.headers = {}
        else:
            resp.json.return_value = {}
        return resp

    return _api_get


def _stub_api_post():
    """Best-effort POST stub for `applied` audit event."""
    def _api_post(path, *args, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"status": "ok"}
        return resp
    return _api_post


def test_init_falls_through_on_404(tmp_path, monkeypatch):
    """Old server returns 404 → default flow runs unchanged.

    This is the regression check that the override probe doesn't break
    backwards-compat with servers that pre-date this feature.
    """
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_cfg"))
    api_get = _build_api_get(initial_workspace_status=None)
    monkeypatch.setattr("cli.commands.init.api_get", api_get, raising=False)
    monkeypatch.setattr("cli.lib.pull.api_get", api_get, raising=False)
    monkeypatch.setattr("cli.lib.initial_workspace.api_get", api_get, raising=False)

    result = runner.invoke(init_app, [
        "--server-url", "http://test.example.com",
        "--token", "t",
        "--workspace", str(tmp_path),
    ])
    assert result.exit_code == 0, result.output
    # Default flow wrote the Agnes CLAUDE.md + settings.json + AGNES_WORKSPACE.md
    assert (tmp_path / "CLAUDE.md").read_text() == "# Default CLAUDE.md\n"
    assert (tmp_path / ".claude" / "settings.json").exists()
    assert (tmp_path / "AGNES_WORKSPACE.md").exists()
    # No override fields in sentinel
    sentinel = (tmp_path / ".claude" / "init-complete").read_text()
    assert "override: true" not in sentinel


def test_init_falls_through_on_configured_false(tmp_path, monkeypatch):
    """200 with configured:false → default flow same as 404."""
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_cfg"))
    api_get = _build_api_get(initial_workspace_status={"configured": False})
    monkeypatch.setattr("cli.commands.init.api_get", api_get, raising=False)
    monkeypatch.setattr("cli.lib.pull.api_get", api_get, raising=False)
    monkeypatch.setattr("cli.lib.initial_workspace.api_get", api_get, raising=False)

    result = runner.invoke(init_app, [
        "--server-url", "http://x",
        "--token", "t",
        "--workspace", str(tmp_path),
    ])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "CLAUDE.md").read_text() == "# Default CLAUDE.md\n"


def test_init_override_extracts_and_writes_extended_sentinel(tmp_path, monkeypatch):
    """configured:true + synced:true on empty workspace → override flow runs.

    Result: admin's CLAUDE.md content lands (not the /api/welcome default),
    Agnes-default files (settings.json, AGNES_WORKSPACE.md, CLAUDE.local.md)
    are NOT written by Agnes, sentinel has override:true.
    """
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_cfg"))
    zip_bytes = _make_zip({
        "CLAUDE.md": b"# Custom Groupon Workspace\n",
        "docs/handbook.md": b"# Handbook\n",
    })
    status = {
        "configured": True,
        "synced": True,
        "template_source": "https://github.com/groupon/template",
        "template_sha": "abc123",
        "synced_at": "2026-05-13T10:00:00Z",
        "files": ["CLAUDE.md", "docs/handbook.md"],
    }
    api_get = _build_api_get(initial_workspace_status=status, zip_bytes=zip_bytes)
    monkeypatch.setattr("cli.commands.init.api_get", api_get, raising=False)
    monkeypatch.setattr("cli.lib.pull.api_get", api_get, raising=False)
    monkeypatch.setattr("cli.lib.initial_workspace.api_get", api_get, raising=False)
    monkeypatch.setattr("cli.lib.initial_workspace.api_post", _stub_api_post(), raising=False)

    result = runner.invoke(init_app, [
        "--server-url", "http://x",
        "--token", "t",
        "--workspace", str(tmp_path),
    ])
    assert result.exit_code == 0, result.output
    # Admin's CLAUDE.md wins, NOT /api/welcome default
    assert (tmp_path / "CLAUDE.md").read_text() == "# Custom Groupon Workspace\n"
    # File only in template repo appeared
    assert (tmp_path / "docs" / "handbook.md").read_text() == "# Handbook\n"
    # Agnes-default files NOT created by Agnes:
    assert not (tmp_path / ".claude" / "settings.json").exists()
    assert not (tmp_path / ".claude" / "CLAUDE.local.md").exists()
    assert not (tmp_path / "AGNES_WORKSPACE.md").exists()
    # Sentinel carries override metadata
    sentinel = (tmp_path / ".claude" / "init-complete").read_text()
    assert "override: true" in sentinel
    assert "template_source: https://github.com/groupon/template" in sentinel
    assert "template_sha: abc123" in sentinel


def test_init_override_exits_when_synced_false(tmp_path, monkeypatch):
    """configured:true + synced:false → typed initial_workspace_not_synced exit."""
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_cfg"))
    status = {
        "configured": True,
        "synced": False,
        "template_source": "https://github.com/groupon/template",
    }
    api_get = _build_api_get(initial_workspace_status=status)
    monkeypatch.setattr("cli.commands.init.api_get", api_get, raising=False)
    monkeypatch.setattr("cli.lib.pull.api_get", api_get, raising=False)
    monkeypatch.setattr("cli.lib.initial_workspace.api_get", api_get, raising=False)

    result = runner.invoke(init_app, [
        "--server-url", "http://x",
        "--token", "t",
        "--workspace", str(tmp_path),
    ])
    assert result.exit_code != 0
    assert "initial_workspace_not_synced" in (result.output + str(result.stderr_bytes or b""))


def test_init_override_force_with_YES_proceeds(tmp_path, monkeypatch):
    """Re-init existing override workspace with --force + YES → extracts."""
    import typer

    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_cfg"))
    # Pre-create override sentinel + some existing content
    _write_sentinel(tmp_path, "override: true\ntemplate_sha: old\n")
    (tmp_path / "CLAUDE.md").write_text("old content\n")

    zip_bytes = _make_zip({"CLAUDE.md": b"# Refreshed\n"})
    status = {
        "configured": True,
        "synced": True,
        "template_source": "https://github.com/groupon/template",
        "template_sha": "new123",
        "synced_at": "2026-05-13T10:00:00Z",
        "files": ["CLAUDE.md"],
    }
    api_get = _build_api_get(initial_workspace_status=status, zip_bytes=zip_bytes)
    monkeypatch.setattr("cli.commands.init.api_get", api_get, raising=False)
    monkeypatch.setattr("cli.lib.pull.api_get", api_get, raising=False)
    monkeypatch.setattr("cli.lib.initial_workspace.api_get", api_get, raising=False)
    monkeypatch.setattr("cli.lib.initial_workspace.api_post", _stub_api_post(), raising=False)
    monkeypatch.setattr(typer, "prompt", lambda *a, **k: "YES")

    result = runner.invoke(init_app, [
        "--server-url", "http://x",
        "--token", "t",
        "--workspace", str(tmp_path),
        "--force",
    ])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "CLAUDE.md").read_text() == "# Refreshed\n"
    sentinel = (tmp_path / ".claude" / "init-complete").read_text()
    assert "template_sha: new123" in sentinel


def test_init_override_force_with_no_aborts(tmp_path, monkeypatch):
    """Re-init with --force but user types "no" → exit 1, workspace untouched."""
    import typer

    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_cfg"))
    _write_sentinel(tmp_path, "override: true\ntemplate_sha: old\n")
    (tmp_path / "CLAUDE.md").write_text("old content\n")

    zip_bytes = _make_zip({"CLAUDE.md": b"# Refreshed\n"})
    status = {
        "configured": True,
        "synced": True,
        "template_sha": "new123",
        "synced_at": "2026-05-13T10:00:00Z",
        "files": ["CLAUDE.md"],
    }
    api_get = _build_api_get(initial_workspace_status=status, zip_bytes=zip_bytes)
    monkeypatch.setattr("cli.commands.init.api_get", api_get, raising=False)
    monkeypatch.setattr("cli.lib.pull.api_get", api_get, raising=False)
    monkeypatch.setattr("cli.lib.initial_workspace.api_get", api_get, raising=False)
    monkeypatch.setattr(typer, "prompt", lambda *a, **k: "no")

    result = runner.invoke(init_app, [
        "--server-url", "http://x",
        "--token", "t",
        "--workspace", str(tmp_path),
        "--force",
    ])
    assert result.exit_code == 1
    # Original content survived
    assert (tmp_path / "CLAUDE.md").read_text() == "old content\n"


def test_init_override_existing_workspace_no_force_exits_partial_state(tmp_path, monkeypatch):
    """Re-init override workspace WITHOUT --force → existing partial_state path."""
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "_cfg"))
    _write_sentinel(tmp_path, "override: true\ntemplate_sha: old\n")
    (tmp_path / "CLAUDE.md").write_text("groupon content\n")

    status = {
        "configured": True,
        "synced": True,
        "template_sha": "new123",
        "synced_at": "2026-05-13T10:00:00Z",
        "files": ["CLAUDE.md"],
    }
    api_get = _build_api_get(initial_workspace_status=status)
    monkeypatch.setattr("cli.commands.init.api_get", api_get, raising=False)
    monkeypatch.setattr("cli.lib.pull.api_get", api_get, raising=False)
    monkeypatch.setattr("cli.lib.initial_workspace.api_get", api_get, raising=False)

    result = runner.invoke(init_app, [
        "--server-url", "http://x",
        "--token", "t",
        "--workspace", str(tmp_path),
    ])
    assert result.exit_code == 1
    assert "partial_state" in (result.output + str(result.stderr_bytes or b""))
    # CLAUDE.md untouched
    assert (tmp_path / "CLAUDE.md").read_text() == "groupon content\n"
