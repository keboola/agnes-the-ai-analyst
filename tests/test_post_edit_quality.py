"""Behavior tests for scripts/post-edit-quality.sh (PostToolUse quality hook).

Drives the script as a subprocess with a hook-style JSON payload on stdin and
asserts: non-.py paths pass through, a missing path is a no-op, a fixable .py
file is reformatted + passes, and an unfixable .py file blocks (exit != 0).
ruff-dependent cases skip when ruff is not on PATH (e.g. minimal CI images).
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
HOOK = REPO_ROOT / "scripts" / "post-edit-quality.sh"
RUFF = shutil.which("ruff")


def run_hook(payload: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(HOOK)],
        input=payload,
        text=True,
        capture_output=True,
        cwd=str(REPO_ROOT),
    )


def run_hook_for(file_path: str) -> subprocess.CompletedProcess:
    return run_hook(json.dumps({"tool_input": {"file_path": file_path}}))


def test_hook_exists():
    assert HOOK.exists(), "scripts/post-edit-quality.sh must exist"


def test_missing_file_path_is_noop():
    result = run_hook('{"tool_input":{}}')
    assert result.returncode == 0, result.stderr


def test_non_python_file_passes_through(tmp_path):
    f = tmp_path / "note.md"
    f.write_text("# hi\n", encoding="utf-8")
    result = run_hook_for(str(f))
    assert result.returncode == 0, result.stderr
    assert f.read_text(encoding="utf-8") == "# hi\n"


@pytest.mark.skipif(RUFF is None, reason="ruff not on PATH")
def test_fixable_python_file_is_formatted_and_passes(tmp_path):
    f = tmp_path / "messy.py"
    f.write_text("x=1\n", encoding="utf-8")
    result = run_hook_for(str(f))
    assert result.returncode == 0, result.stderr
    assert f.read_text(encoding="utf-8") == "x = 1\n"


@pytest.mark.skipif(RUFF is None, reason="ruff not on PATH")
def test_unfixable_python_file_blocks(tmp_path):
    f = tmp_path / "broken.py"
    f.write_text("def broken(:\n    pass\n", encoding="utf-8")  # E999 syntax error
    result = run_hook_for(str(f))
    assert result.returncode != 0


def test_settings_wires_post_edit_hook():
    settings = REPO_ROOT / ".claude" / "settings.json"
    assert settings.exists(), ".claude/settings.json must exist"
    data = json.loads(settings.read_text(encoding="utf-8"))
    entries = data.get("hooks", {}).get("PostToolUse", [])
    assert entries, "PostToolUse hooks must be configured"
    commands = [
        h.get("command", "")
        for entry in entries
        for h in entry.get("hooks", [])
    ]
    assert any("post-edit-quality.sh" in c for c in commands), (
        "a PostToolUse hook must invoke scripts/post-edit-quality.sh"
    )
    matchers = [entry.get("matcher", "") for entry in entries]
    assert any("Edit" in m for m in matchers), "matcher must cover Edit"
