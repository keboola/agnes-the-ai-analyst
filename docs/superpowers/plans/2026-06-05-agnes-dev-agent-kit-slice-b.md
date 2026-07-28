# Agnes Dev-Agent Kit — Slice B (Quality Hook) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A PostToolUse quality hook that auto-runs ruff (fix + format, blocking) and mypy (advisory) on every Python file Claude Code edits, wired via a committed `.claude/settings.json`.

**Architecture:** A dependency-light bash script (`scripts/post-edit-quality.sh`) reads the hook's tool-input JSON from stdin, extracts the edited file path, and — for `.py` files — runs ruff then mypy. Tool discovery is robust (`.venv/bin` → PATH → `uv run`); a missing tool is skipped, never an error. ruff issues that survive `--fix` make the hook exit non-zero (Claude must address them); mypy is advisory and never blocks. `.claude/settings.json` (committed, with a `.gitignore` exception) wires the hook to `Edit|Write|MultiEdit`. A pytest subprocess-drives the script to verify behavior.

**Tech Stack:** bash, `python3` (stdlib `json` for stdin parse), ruff, mypy, pytest.

Spec: `docs/superpowers/specs/2026-06-05-agnes-dev-agent-kit-design.md` §9.

---

## File Structure

- Create: `scripts/post-edit-quality.sh` — the hook (one responsibility: lint/format the one edited file).
- Create: `tests/test_post_edit_quality.py` — subprocess behavior tests + settings-wiring guard.
- Create: `.claude/settings.json` — PostToolUse wiring (committed).
- Modify: `.gitignore` — add `!.claude/settings.json` exception.
- Modify: `CHANGELOG.md` — `## [Unreleased]` bullet.

Note on environment (verified at 0.66.1): ruff is NOT in `.venv` (it's on PATH via homebrew); mypy is not reliably present. The hook must degrade gracefully. Tests that need ruff skip themselves when ruff is absent from PATH.

---

## Task 1: The hook script + behavior tests

**Files:**
- Create: `tests/test_post_edit_quality.py`
- Create: `scripts/post-edit-quality.sh`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_post_edit_quality.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_post_edit_quality.py -v`
Expected: FAIL — `test_hook_exists` asserts (script not created yet); subprocess cases error because the script is missing.

- [ ] **Step 3: Create `scripts/post-edit-quality.sh`**

Create the file with EXACTLY this content, then make it executable (`chmod +x scripts/post-edit-quality.sh`):

```bash
#!/usr/bin/env bash
# Post-edit quality gate: ruff fix/format (blocking) + mypy (advisory) on the
# single Python file just edited. Invoked by Claude Code's PostToolUse hook
# (.claude/settings.json) after Edit/Write/MultiEdit. Reads the tool-input JSON
# from stdin (Claude Code hook contract).
#
# Exit 0 = continue. Non-zero = Claude sees the failure as a tool result and must
# address it before its next action. mypy is advisory and never blocks (mirrors
# CI's continue-on-error posture). Missing tools are skipped, not errors.
#
# Manual debug:
#   echo '{"tool_input":{"file_path":"src/db.py"}}' | scripts/post-edit-quality.sh
set -euo pipefail

PAYLOAD="$(cat)"

FILE_PATH="$(printf '%s' "$PAYLOAD" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("tool_input",{}).get("file_path",""))' \
    2>/dev/null || true)"

[ -z "$FILE_PATH" ] && exit 0
case "$FILE_PATH" in
    *.py) ;;
    *) exit 0 ;;
esac
[ -f "$FILE_PATH" ] || exit 0

# Run from the repo root when the file is in a git repo (so pyproject.toml config
# applies); otherwise fall back to the file's directory.
REPO_ROOT="$(git -C "$(dirname "$FILE_PATH")" rev-parse --show-toplevel 2>/dev/null || true)"
cd "${REPO_ROOT:-$(dirname "$FILE_PATH")}"

# Locate ruff: prefer the venv, then PATH, then uv.
if [ -x ".venv/bin/ruff" ]; then
    RUFF=(.venv/bin/ruff)
elif command -v ruff >/dev/null 2>&1; then
    RUFF=(ruff)
elif command -v uv >/dev/null 2>&1; then
    RUFF=(uv run --quiet ruff)
else
    RUFF=()
fi

FAILED=0

if [ "${#RUFF[@]}" -gt 0 ]; then
    if ! "${RUFF[@]}" check --fix --quiet "$FILE_PATH"; then
        echo "post-edit: ruff found unresolved issues in $FILE_PATH" >&2
        FAILED=1
    fi
    "${RUFF[@]}" format --quiet "$FILE_PATH" || true
else
    echo "post-edit: ruff not found; skipping lint/format" >&2
fi

# mypy: advisory only. Never blocks. Skipped if not installed.
if [ -x ".venv/bin/mypy" ]; then
    MYPY=(.venv/bin/mypy)
elif command -v mypy >/dev/null 2>&1; then
    MYPY=(mypy)
else
    MYPY=()
fi

if [ "${#MYPY[@]}" -gt 0 ]; then
    MYPY_OUTPUT="$("${MYPY[@]}" --ignore-missing-imports --no-error-summary "$FILE_PATH" 2>&1 || true)"
    if [ -n "$MYPY_OUTPUT" ]; then
        printf '%s\n' "$MYPY_OUTPUT" | head -10 >&2
    fi
fi

exit $FAILED
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_post_edit_quality.py -v`
Expected: PASS (3 always-on tests pass; the 2 ruff cases pass if ruff is on PATH, else SKIPPED).

- [ ] **Step 5: Commit**

```bash
git add tests/test_post_edit_quality.py scripts/post-edit-quality.sh
git commit -m "feat(dev-kit): add post-edit ruff/mypy quality hook script"
```

---

## Task 2: Wire the hook in committed settings + gitignore exception

**Files:**
- Modify: `tests/test_post_edit_quality.py` (APPEND a test)
- Create: `.claude/settings.json`
- Modify: `.gitignore`

- [ ] **Step 1: Append the failing test**

Append to the END of `tests/test_post_edit_quality.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_post_edit_quality.py::test_settings_wires_post_edit_hook -v`
Expected: FAIL — `.claude/settings.json must exist` (it is currently gitignored and absent).

- [ ] **Step 3: Add the gitignore exception**

In `.gitignore`, find the block:

```
.claude/*
!.claude/agents/
!.claude/skills/
!.claude/commands/
```

Add one line after the last exception so it reads:

```
.claude/*
!.claude/agents/
!.claude/skills/
!.claude/commands/
!.claude/settings.json
```

(Do NOT add an exception for `settings.local.json` — that stays personal/gitignored.)

- [ ] **Step 4: Create `.claude/settings.json`**

Create the file with EXACTLY this content:

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Edit|Write|MultiEdit",
        "hooks": [
          {
            "type": "command",
            "command": "$CLAUDE_PROJECT_DIR/scripts/post-edit-quality.sh"
          }
        ]
      }
    ]
  }
}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_post_edit_quality.py -v`
Expected: PASS (the new test passes; earlier tests still pass/skip as before).

- [ ] **Step 6: Commit**

```bash
git add tests/test_post_edit_quality.py .claude/settings.json .gitignore
git commit -m "feat(dev-kit): wire post-edit quality hook in committed settings"
```

---

## Task 3: Full-suite check + CHANGELOG

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Run the slice's tests**

Run: `.venv/bin/pytest tests/test_post_edit_quality.py -v`
Expected: PASS (skips allowed for ruff cases only when ruff is absent).

- [ ] **Step 2: Run the full suite to confirm no regressions**

Run: `.venv/bin/pytest tests/ --tb=short -n auto -q`
Expected: baseline (the pre-existing failures `tests/test_setup_instructions.py::test_install_page_uses_versioned_wheel_url` and `tests/test_mcp_server.py::TestMCPProtocol::test_server_info_in_initialize_response` are unrelated to this slice — do not fix them; confirm no NEW failures in files this slice touched).

- [ ] **Step 3: Add a CHANGELOG bullet**

Under `## [Unreleased]` in `CHANGELOG.md`, in the `### Added` group (create it if absent), add:

```markdown
- Dev-agent kit (quality hook): a PostToolUse hook (`scripts/post-edit-quality.sh`, wired in `.claude/settings.json`) auto-runs ruff fix/format (blocking) and mypy (advisory) on each edited Python file.
```

- [ ] **Step 4: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): dev-agent kit quality hook"
```

---

## Self-review notes

- **Spec coverage (§9):** ruff fix/format blocking + mypy advisory + no pytest → Task 1 hook; `.claude/settings.json` wiring → Task 2; manual-invocation contract → hook header comment; behavior tests → Task 1; CHANGELOG → Task 3.
- **No placeholders:** every step has full file content and exact commands.
- **Environment realism:** the hook discovers ruff/mypy across venv/PATH/uv and degrades to skip; tests skip ruff cases when ruff is absent, so CI without ruff stays green.
- **Robustness choices:** hook falls back to the file's directory when the file is not in a git repo (lets the subprocess tests use `tmp_path`); mypy output is advisory and never sets a non-zero exit.
- **Out of scope (later slices):** builder + `agnes-conventions` (C), router + thin/fat (D), build team (E).
