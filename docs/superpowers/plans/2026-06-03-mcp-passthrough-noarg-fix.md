# MCP Passthrough Zero-Argument Tool Fix — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix passthrough MCP tools that declare no input parameters so they no longer fail FastMCP validation with `kwargs Field required`.

**Architecture:** Both passthrough-tool generators synthesize a Python callable whose signature FastMCP turns into a Pydantic validation model. A tool with an empty `input_schema.properties` is wrongly routed into the `**kwargs` wrapper branch; FastMCP renders `**kwargs` as a *required* `kwargs` object, so the only valid (empty) call fails. The synthesized-signature path already handles the empty case correctly (it emits a parameterless `def`), so the fix is to stop diverting empty-schema tools into the `**kwargs` branch.

**Tech Stack:** Python 3.12, FastMCP (`mcp` SDK), pytest. Two sibling generators: `cli/mcp/_dynamic_passthrough.py` (analyst-laptop stdio server, sync) and `app/api/mcp/tools_generator.py` (server-hosted FastMCP, async).

**Root cause (verified):** in both files the branch reads
`if fallback_kwargs or not safe_props:` → `**kwargs` wrapper.
`fallback_kwargs = len(safe_props) != len(props)` is already `True` whenever some property names are non-identifiers (including the all-unsafe case). The extra `or not safe_props` only adds the **zero-property** case (`len(props)==0`), which is exactly the bug. Dropping `or not safe_props` lets empty-schema tools fall through to the synthesized-signature path, which produces a valid parameterless `def _passthrough():` / `async def _passthrough():`.

**Out of scope:** the separate "stdio MCP sources can only inject one env var (`auth_secret_env`)" gap — tracked separately, not part of this fix.

---

## File Structure

- Modify: `app/api/mcp/tools_generator.py:72` — drop `or not safe_props` from the branch condition; add a clarifying comment.
- Modify: `cli/mcp/_dynamic_passthrough.py:63` — same change.
- Test: `tests/test_mcp_cli_dynamic_passthrough.py` — add a no-arg registration test (CLI side).
- Test: `tests/test_mcp_tools_generator.py` (new) — add a no-arg test (server side; no existing test file for this module).
- Modify: `CHANGELOG.md` — `### Fixed` bullet under `## [Unreleased]`.
- Modify: `pyproject.toml` — version bump `0.59.1` → `0.59.2` (release-cut, last commit).

---

## Task 1: Isolated worktree

**Files:** none (git/workspace setup)

- [ ] **Step 1: Create an isolated worktree + branch**

REQUIRED SUB-SKILL: `superpowers:using-git-worktrees`. Create a worktree for branch `fix/mcp-passthrough-noarg` and `cd` into it. Do NOT work in the main checkout (it is used in parallel). All subsequent steps run inside the worktree.

- [ ] **Step 2: Sanity — confirm the two generators still carry the bug**

Run: `grep -n "fallback_kwargs or not safe_props" app/api/mcp/tools_generator.py cli/mcp/_dynamic_passthrough.py`
Expected: one match in each file (lines ~72 and ~63).

---

## Task 2: Server-side generator — failing test first (`tools_generator.py`)

**Files:**
- Create: `tests/test_mcp_tools_generator.py`
- Modify: `app/api/mcp/tools_generator.py:72`

- [ ] **Step 1: Write the failing test**

Create `tests/test_mcp_tools_generator.py`:

```python
"""Server-side passthrough callable synthesis (app/api/mcp/tools_generator.py).

Regression guard: a tool with an EMPTY input schema must register a
parameterless callable, NOT a ``**kwargs`` wrapper. FastMCP renders
``**kwargs`` as a required ``kwargs`` field, so empty (the only valid) calls
to a no-arg tool would 422.
"""
from __future__ import annotations

import pytest

pytest.importorskip("mcp", reason="mcp SDK not installed")

from app.api.mcp.tools_generator import _make_passthrough_callable
from mcp.server.fastmcp import FastMCP


def _register(input_schema):
    """Build the callable for a fake source/tool and mount it on a fresh
    FastMCP, returning the registered Tool object."""
    source = {"id": "src1", "name": "fake", "transport": "stdio", "command": "/bin/true"}
    fn = _make_passthrough_callable(source, "noarg_tool", input_schema)
    mcp = FastMCP("Test", instructions="t")
    mcp.add_tool(fn, name="noarg_tool")
    tools = mcp._tool_manager.list_tools()
    return next(t for t in tools if t.name == "noarg_tool")


def test_empty_schema_registers_no_kwargs_param():
    tool = _register({"type": "object", "properties": {}})
    props = (tool.parameters or {}).get("properties") or {}
    required = (tool.parameters or {}).get("required") or []
    assert "kwargs" not in props, f"unexpected kwargs param: {tool.parameters}"
    assert "kwargs" not in required


def test_none_schema_registers_no_kwargs_param():
    tool = _register(None)
    props = (tool.parameters or {}).get("properties") or {}
    assert "kwargs" not in props


def test_unsafe_prop_names_still_use_kwargs():
    # Regression guard: genuinely non-identifier prop names must still fall
    # back to the kwargs wrapper (this path is unchanged by the fix).
    tool = _register({"type": "object", "properties": {"weird-key": {"type": "string"}}})
    props = (tool.parameters or {}).get("properties") or {}
    assert "kwargs" in props
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_mcp_tools_generator.py -v`
Expected: `test_empty_schema_registers_no_kwargs_param` and `test_none_schema_registers_no_kwargs_param` FAIL (a `kwargs` property is present); `test_unsafe_prop_names_still_use_kwargs` PASSES.

- [ ] **Step 3: Apply the minimal fix**

In `app/api/mcp/tools_generator.py`, change line 72 from:

```python
    if fallback_kwargs or not safe_props:
```

to:

```python
    # NOTE: only the genuine non-identifier case takes the **kwargs wrapper.
    # An EMPTY schema (no props) must fall through to the synthesized path
    # below, which emits a valid parameterless ``async def _passthrough():``.
    # Routing empty schemas here instead makes FastMCP render a *required*
    # ``kwargs`` field, so the only valid (empty) call 422s.
    if fallback_kwargs:
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/pytest tests/test_mcp_tools_generator.py -v`
Expected: all three PASS.

- [ ] **Step 5: Commit**

```bash
git add app/api/mcp/tools_generator.py tests/test_mcp_tools_generator.py
git commit -m "fix(mcp): empty-schema passthrough tools register parameterless (server)"
```

---

## Task 3: CLI generator — failing test first (`_dynamic_passthrough.py`)

**Files:**
- Modify: `tests/test_mcp_cli_dynamic_passthrough.py`
- Modify: `cli/mcp/_dynamic_passthrough.py:63`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_mcp_cli_dynamic_passthrough.py`:

```python
def test_registered_noarg_tool_has_no_kwargs_param():
    """A passthrough tool with an empty input schema must register a
    parameterless FastMCP tool — never a required ``kwargs`` field."""
    mcp_inst = _fresh_mcp()
    tool_list = [
        {
            "tool_id": "src1.noarg_tool",
            "source_name": "src1",
            "exposed_name": "noarg_tool",
            "description": "a tool with no params",
            "input_schema": {"type": "object", "properties": {}},
        }
    ]
    with patch(
        "cli.mcp._dynamic_passthrough.api_get_json",
        return_value={"tools": tool_list},
    ):
        registered = register_passthrough_tools(mcp_inst)
    assert registered  # at least one tool registered
    tools = mcp_inst._tool_manager.list_tools()
    tool = next(t for t in tools if "noarg_tool" in t.name)
    props = (tool.parameters or {}).get("properties") or {}
    required = (tool.parameters or {}).get("required") or []
    assert "kwargs" not in props, f"unexpected kwargs param: {tool.parameters}"
    assert "kwargs" not in required
```

NOTE: match the GET shape the existing tests use. If `_sample_tool_list()` / the patched function differs (e.g. the helper returns a bare list or the patch target is `api_get_json` returning `{"tools": [...]}`), mirror that exact shape — read `test_register_registers_each_tool_with_namespaced_name` (line ~90) and copy its construction verbatim, swapping the tool entry for the empty-schema one above.

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_mcp_cli_dynamic_passthrough.py::test_registered_noarg_tool_has_no_kwargs_param -v`
Expected: FAIL — `kwargs` present in the tool's parameters.

- [ ] **Step 3: Apply the minimal fix**

In `cli/mcp/_dynamic_passthrough.py`, change line 63 from:

```python
    if fallback_kwargs or not safe_props:
```

to:

```python
    # Empty schema (no props) must fall through to the synthesized
    # parameterless ``def _passthrough():`` below — only genuine
    # non-identifier prop names take the **kwargs wrapper. (FastMCP renders
    # **kwargs as a required field, which breaks no-arg calls.)
    if fallback_kwargs:
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/pytest tests/test_mcp_cli_dynamic_passthrough.py -v`
Expected: the new test PASSES and all pre-existing tests in the file still PASS (esp. `test_registered_kwargs_fallback_for_unsafe_prop_names`).

- [ ] **Step 5: Commit**

```bash
git add cli/mcp/_dynamic_passthrough.py tests/test_mcp_cli_dynamic_passthrough.py
git commit -m "fix(mcp): empty-schema passthrough tools register parameterless (cli)"
```

---

## Task 4: Changelog

**Files:**
- Modify: `CHANGELOG.md` (under `## [Unreleased]` → `### Fixed`)

- [ ] **Step 1: Add the Fixed bullet**

Under `## [Unreleased]` → `### Fixed`, add:

```markdown
- MCP passthrough tools with no input parameters (e.g. canned-view tools like a pipeline summary) no longer fail with a `kwargs` validation error. Empty-schema tools now register a parameterless signature in both the server-hosted and CLI stdio MCP servers instead of a `**kwargs` wrapper that FastMCP rendered as a required field.
```

- [ ] **Step 2: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): note empty-schema passthrough fix"
```

---

## Task 5: Full verification + reviewers

**Files:** none

- [ ] **Step 1: Run the full test suite (what CI runs)**

Run: `.venv/bin/pytest tests/ --tb=short -n auto -q`
Expected: green. Failures in code you touched → fix before proceeding. Pre-existing unrelated failures → confirm with `git stash` they reproduce on a clean branch, note them, don't block.

- [ ] **Step 2: Run the rules reviewer**

Dispatch the `agnes-reviewer-rules` subagent on the diff (CHANGELOG discipline, vendor-agnostic content, no AI attribution, clean commits, issue economy). Address any blocking findings, then re-run Step 1 if code changed.

(Architecture/RBAC reviewers are NOT triggered: the diff touches `app/api/mcp/tools_generator.py` + `cli/mcp/` only — no `extract.duckdb`/orchestrator/db/migration changes, and no new endpoint/auth gate or `ResourceType`.)

---

## Task 6: Release-cut (LAST commit on the PR)

**Files:**
- Modify: `pyproject.toml` (version `0.59.1` → `0.59.2`)
- Modify: `CHANGELOG.md` (rename `[Unreleased]` → `[0.59.2] - <date>`, add fresh empty `[Unreleased]`)

- [ ] **Step 1: Re-read the live release recipe**

Read `docs/RELEASING.md` and the CLAUDE.md "Release process" section — the procedure evolves; follow the current version, not this plan's summary, if they diverge.

- [ ] **Step 2: Bump version**

In `pyproject.toml`, change `version = "0.59.1"` → `version = "0.59.2"`. (Patch bump — bugfix, no new public surface. If anything here looks minor-worthy, STOP and ask the user before bumping minor.)

- [ ] **Step 3: Rename the changelog section**

In `CHANGELOG.md`, rename `## [Unreleased]` to `## [0.59.2] - <today's date>` (keep the `### Fixed` bullet under it), and insert a fresh empty `## [Unreleased]` block (Added/Changed/Fixed/Removed/Internal) above it.

- [ ] **Step 4: Commit the release-cut**

```bash
git add pyproject.toml CHANGELOG.md
git commit -m "chore(release): 0.59.2"
```

---

## Task 7: PR (merge gated on explicit user go-ahead)

**Files:** none

- [ ] **Step 1: Push + open the PR**

```bash
git push -u origin fix/mcp-passthrough-noarg
gh pr create --title "fix(mcp): empty-schema passthrough tools register parameterless" --body "<why: faithful end-to-end test of Agnes MCP Sources found that no-parameter passthrough tools 422'd via FastMCP; fix routes empty schemas to the synthesized parameterless signature in both generators; tests added; release-cut 0.59.2 included>"
```
PR body must be vendor-agnostic — no customer tokens/hostnames; no AI attribution.

- [ ] **Step 2: STOP — do not merge.**

Merging requires an explicit "mergni" from the user (deploy ≠ merge). Report PR URL + CI status and wait. After the user says merge: merge, then tag `v0.59.2` on the merge commit + create the GitHub Release, and watch the post-merge `release.yml` smoke-test (green + `rollback-on-smoke-fail` skipped).

---

## Self-Review

- **Spec coverage:** root cause (both files) → Tasks 2 & 3; tests → Tasks 2 & 3; changelog → Task 4; verification + review → Task 5; release-cut → Task 6; PR/merge → Task 7. ✓
- **Placeholder scan:** the only deliberately conditional content is the CLI test's "mirror the existing GET shape" note (Task 3 Step 1) — the existing-test construction is the source of truth; the fix code and server test are fully concrete. ✓
- **Type/name consistency:** the condition change is identical in both files (`if fallback_kwargs:`); test assertions use `tool.parameters` / `_tool_manager.list_tools()`, matching the existing CLI test's `_tool_manager` usage. ✓
