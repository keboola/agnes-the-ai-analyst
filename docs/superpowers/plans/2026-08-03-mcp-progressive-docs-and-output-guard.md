# MCP Progressive Tool Docs + Output-Size Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Shrink the MCP `tools/list` context load via first-paragraph descriptions + an on-demand `tool_docs` tool, and hard-cap serialized output of the MCP data tools with actionable narrowing guidance.

**Architecture:** One new shared module `src/mcp_tooling.py` (importable from both `app/` and `cli/` — precedent: `cli/mcp/server.py` already imports `src.duckdb_conn`) provides `summarize_docstring`, a `progressive_tool` decorator factory, and `ensure_output_size`. Both MCP servers (`app/api/mcp/foundation_tools.py` for the HTTP transports, `cli/mcp/server.py` for stdio) adopt the decorator, register their own `tool_docs` tool over a per-module registry dict, and wrap their data tools' returns in the guard.

**Tech Stack:** Python, `mcp.server.fastmcp.FastMCP`, pytest.

**Spec:** `docs/superpowers/specs/2026-08-03-mcp-progressive-docs-and-output-guard-design.md`

## Global Constraints

- Output cap default: **100 000 chars** of serialized JSON; env override `AGNES_MCP_MAX_OUTPUT_CHARS`; `0` (or negative) disables; invalid value falls back to default. Cap is read at call time, not import time.
- Over-cap behavior: raise (no partial payload). Exception type `MCPOutputTooLarge(ValueError)`.
- Guarded tools: server `query`, `describe`; CLI `query`, `query_local`, `describe`. Nothing else.
- Wire description = first docstring paragraph; if the docstring has more content, append exactly ` Full contract: tool_docs('<name>').`
- `tool_docs(tool_name)` returns `{"tool": "<name>", "docs": "<full docstring>"}`; unknown name raises `ValueError` listing valid names.
- Ratchet: every registered wire description ≤ **500 chars**. If an existing first paragraph exceeds 500, trim that docstring's first paragraph (content edit is allowed and expected).
- Docstrings in source stay full — only the wire description changes.
- No REST API changes, no `agnes query` CLI-command changes, no admin-UI knob.
- Repo rules: CHANGELOG bullet in the same PR; no AI attribution in commits; stage explicit paths (never `git add -A`); vendor-neutral code comments.
- Test runner: `.venv/bin/pytest` (worktree `.venv` is a symlink to the main checkout's venv — create it first if missing, see Task 1 Step 0). Do NOT run the full suite locally; CI does that (draft PR opens after the first push).

---

### Task 1: Shared module — `summarize_docstring` + output guard

**Files:**
- Create: `src/mcp_tooling.py`
- Test: `tests/test_mcp_tooling.py`

**Interfaces:**
- Produces: `summarize_docstring(doc: str | None) -> tuple[str, bool]`; `ensure_output_size(payload, tool_name, *, hint=QUERY_NARROW_HINT, cap=None)`; `max_output_chars() -> int`; constants `DEFAULT_MAX_OUTPUT_CHARS = 100_000`, `MAX_OUTPUT_CHARS_ENV = "AGNES_MCP_MAX_OUTPUT_CHARS"`, `QUERY_NARROW_HINT`; exception `MCPOutputTooLarge(ValueError)`. Tasks 2–5 rely on these exact names.

- [ ] **Step 0: Ensure the worktree venv symlink exists**

```bash
[ -e .venv ] || ln -s ../../../.venv .venv
.venv/bin/python -c "import pytest; print('ok')"
```

(The worktree lives at `.claude/worktrees/<name>/` inside the main checkout, so `../../../.venv` resolves to the main checkout's venv. Never create a fresh venv in a worktree.)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_mcp_tooling.py`:

```python
"""Unit tests for src/mcp_tooling.py — MCP wire-description summarizer and
output-size guard shared by the HTTP foundation and CLI stdio MCP servers."""

from __future__ import annotations

from datetime import date

import pytest

from src.mcp_tooling import (
    DEFAULT_MAX_OUTPUT_CHARS,
    MAX_OUTPUT_CHARS_ENV,
    MCPOutputTooLarge,
    ensure_output_size,
    max_output_chars,
    summarize_docstring,
)


class TestSummarizeDocstring:
    def test_single_paragraph_no_more(self):
        assert summarize_docstring("One line only.") == ("One line only.", False)

    def test_multi_paragraph_returns_first_and_flags_more(self):
        doc = "First para line one\ncontinues here.\n\nSecond paragraph."
        summary, has_more = summarize_docstring(doc)
        assert summary == "First para line one continues here."
        assert has_more is True

    def test_indented_docstring_is_cleaned(self):
        doc = """Show schema plus sample rows.

        Args:
            table_id: Table ID.
        """
        summary, has_more = summarize_docstring(doc)
        assert summary == "Show schema plus sample rows."
        assert has_more is True

    def test_none_and_empty(self):
        assert summarize_docstring(None) == ("", False)
        assert summarize_docstring("   ") == ("", False)


class TestEnsureOutputSize:
    def test_under_cap_returns_payload_unchanged(self):
        payload = {"rows": [[1, 2]]}
        assert ensure_output_size(payload, "query", cap=1000) is payload

    def test_over_cap_raises_with_guidance(self):
        payload = {"rows": [["x" * 500]]}
        with pytest.raises(MCPOutputTooLarge) as exc:
            ensure_output_size(payload, "query", cap=100)
        msg = str(exc.value)
        assert "query response" in msg
        assert "output cap" in msg
        assert "WHERE" in msg  # default hint mentions narrowing options

    def test_custom_hint_lands_in_message(self):
        with pytest.raises(MCPOutputTooLarge) as exc:
            ensure_output_size({"x": "y" * 200}, "describe", hint="lower `rows`", cap=50)
        assert "lower `rows`" in str(exc.value)

    def test_cap_zero_disables(self):
        payload = {"rows": [["x" * 10_000]]}
        assert ensure_output_size(payload, "query", cap=0) is payload

    def test_non_json_values_measured_via_str(self):
        payload = {"rows": [[date(2026, 1, 1)]]}
        assert ensure_output_size(payload, "query", cap=1000) is payload


class TestMaxOutputChars:
    def test_default(self, monkeypatch):
        monkeypatch.delenv(MAX_OUTPUT_CHARS_ENV, raising=False)
        assert max_output_chars() == DEFAULT_MAX_OUTPUT_CHARS

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv(MAX_OUTPUT_CHARS_ENV, "5000")
        assert max_output_chars() == 5000

    def test_invalid_env_falls_back(self, monkeypatch):
        monkeypatch.setenv(MAX_OUTPUT_CHARS_ENV, "banana")
        assert max_output_chars() == DEFAULT_MAX_OUTPUT_CHARS

    def test_env_zero_disables_guard(self, monkeypatch):
        monkeypatch.setenv(MAX_OUTPUT_CHARS_ENV, "0")
        big = {"rows": [["x" * (DEFAULT_MAX_OUTPUT_CHARS + 10)]]}
        assert ensure_output_size(big, "query") is big
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_mcp_tooling.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.mcp_tooling'`

- [ ] **Step 3: Write the implementation**

Create `src/mcp_tooling.py`:

```python
"""Shared helpers for the Agnes MCP servers (HTTP foundation + CLI stdio).

Two concerns, both about keeping MCP tool traffic inside a model's context
budget:

- ``summarize_docstring`` / ``progressive_tool`` — ship only the first
  docstring paragraph as the wire description on ``tools/list``; the full
  docstring stays available on demand via each server's ``tool_docs`` tool.
- ``ensure_output_size`` — hard cap on serialized tool output; over the cap
  the tool raises with actionable narrowing guidance instead of returning a
  payload that would flood the model's context.
"""

from __future__ import annotations

import inspect
import json
import os
from typing import Any, Callable, MutableMapping

DEFAULT_MAX_OUTPUT_CHARS = 100_000
MAX_OUTPUT_CHARS_ENV = "AGNES_MCP_MAX_OUTPUT_CHARS"

QUERY_NARROW_HINT = (
    "select specific columns, add a WHERE filter, lower `limit`, or aggregate server-side"
)


class MCPOutputTooLarge(ValueError):
    """Serialized tool output exceeded the configured cap."""


def max_output_chars() -> int:
    """Resolve the output cap: env override, else default. ``0`` disables."""
    raw = os.environ.get(MAX_OUTPUT_CHARS_ENV, "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return DEFAULT_MAX_OUTPUT_CHARS


def ensure_output_size(
    payload: Any,
    tool_name: str,
    *,
    hint: str = QUERY_NARROW_HINT,
    cap: int | None = None,
) -> Any:
    """Return ``payload`` unless its serialized size exceeds the cap.

    Raises :class:`MCPOutputTooLarge` with an actionable message instead of
    returning an oversized payload — no partial data, so an agent never
    computes over a silently-incomplete result.
    """
    effective = max_output_chars() if cap is None else cap
    if effective <= 0:
        return payload
    size = len(json.dumps(payload, default=str))
    if size > effective:
        raise MCPOutputTooLarge(
            f"{tool_name} response is ~{size:,} chars, over the {effective:,}-char "
            f"output cap. Narrow the request: {hint}."
        )
    return payload


def summarize_docstring(doc: str | None) -> tuple[str, bool]:
    """Return ``(first paragraph as one line, has_more_content)``."""
    cleaned = inspect.cleandoc(doc or "").strip()
    if not cleaned:
        return "", False
    first, _sep, rest = cleaned.partition("\n\n")
    summary = " ".join(line.strip() for line in first.splitlines())
    return summary, bool(rest.strip())


def progressive_tool(
    mcp: Any, docs_registry: MutableMapping[str, str]
) -> Callable[[], Callable[[Callable], Callable]]:
    """Drop-in replacement factory for ``@mcp.tool()``.

    ``tool = progressive_tool(mcp, registry)`` then ``@tool()`` registers the
    function with only its first docstring paragraph as the wire description
    (plus a ``tool_docs`` pointer when the docstring has more), and stores the
    full docstring in ``docs_registry`` for the on-demand ``tool_docs`` tool.
    The decorated function is returned unchanged, matching ``FastMCP.tool``.
    """

    def tool() -> Callable[[Callable], Callable]:
        def decorate(fn: Callable) -> Callable:
            full = inspect.cleandoc(fn.__doc__ or "").strip()
            summary, has_more = summarize_docstring(full)
            description = summary or fn.__name__
            if has_more:
                description += f" Full contract: tool_docs('{fn.__name__}')."
            docs_registry[fn.__name__] = full
            return mcp.tool(description=description)(fn)

        return decorate

    return tool
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_mcp_tooling.py -q`
Expected: all PASS (the `progressive_tool` part is tested in Task 2).

- [ ] **Step 5: Commit**

```bash
git add src/mcp_tooling.py tests/test_mcp_tooling.py
git commit -m "feat(mcp): shared docstring summarizer + output-size guard helpers"
```

---

### Task 2: `progressive_tool` decorator against a real FastMCP

**Files:**
- Modify: `tests/test_mcp_tooling.py` (append a test class)
- (Implementation already landed in Task 1 — this task proves it against the real `FastMCP` registration path.)

**Interfaces:**
- Consumes: `progressive_tool(mcp, docs_registry)` from Task 1.
- Produces: proven contract that `mcp._tool_manager.get_tool(name).description` is the summary (+pointer) and `.fn` is the original function — Tasks 3 and 5 depend on both.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mcp_tooling.py`:

```python
from src.mcp_tooling import progressive_tool


class TestProgressiveTool:
    def _mcp(self):
        pytest.importorskip("mcp", reason="mcp package not installed")
        from mcp.server.fastmcp import FastMCP

        return FastMCP("tooling-test")

    def test_wire_description_is_first_paragraph_plus_pointer(self):
        mcp = self._mcp()
        registry: dict[str, str] = {}
        tool = progressive_tool(mcp, registry)

        @tool()
        def sample(x: int = 1) -> dict:
            """Do the thing.

            Args:
                x: A number.
            """
            return {"x": x}

        t = mcp._tool_manager.get_tool("sample")
        assert t.description == "Do the thing. Full contract: tool_docs('sample')."
        assert registry["sample"].startswith("Do the thing.")
        assert "Args:" in registry["sample"]

    def test_single_paragraph_gets_no_pointer(self):
        mcp = self._mcp()
        registry: dict[str, str] = {}
        tool = progressive_tool(mcp, registry)

        @tool()
        def brief() -> dict:
            """Just this."""
            return {}

        assert mcp._tool_manager.get_tool("brief").description == "Just this."

    def test_decorated_function_is_returned_unchanged(self):
        mcp = self._mcp()
        tool = progressive_tool(mcp, {})

        @tool()
        def add_one(x: int) -> dict:
            """Add one.

            More detail.
            """
            return {"x": x + 1}

        # Callable directly (back-compat: mcp_http binds tool fns into globals)
        assert add_one(3) == {"x": 4}
        assert mcp._tool_manager.get_tool("add_one").fn is add_one
```

- [ ] **Step 2: Run tests**

Run: `.venv/bin/pytest tests/test_mcp_tooling.py -q`
Expected: all PASS (implementation exists). If `description` lands differently (e.g. FastMCP appends the docstring), fix `progressive_tool` until these assertions hold — they are the contract Tasks 3/5 build on.

- [ ] **Step 3: Commit**

```bash
git add tests/test_mcp_tooling.py
git commit -m "test(mcp): progressive_tool contract against real FastMCP registration"
```

---

### Task 3: Server adoption — decorator swap + `tool_docs` foundation tool

**Files:**
- Modify: `app/api/mcp/foundation_tools.py` (import, `TOOL_DOCS` registry, `tool = progressive_tool(...)`, 59× decorator swap, new `tool_docs` tool, `FOUNDATION_TOOL_NAMES` entry)
- Modify: `tests/test_mcp_http.py` (exact-set test + new test classes)

**Interfaces:**
- Consumes: `progressive_tool` from Task 1.
- Produces: module-level `TOOL_DOCS: dict[str, str]` in `app.api.mcp.foundation_tools`; foundation tool `tool_docs(tool_name: str) -> dict`; `"tool_docs"` present in `FOUNDATION_TOOL_NAMES` (Task 5's CLI server registers its own sibling; the parity test picks the name up automatically).

- [ ] **Step 1: Write the failing tests**

In `tests/test_mcp_http.py`, add `"tool_docs"` to the exact-set assertion in `test_exact_server_side_tool_set` (the big `assert tools == {...}` literal — add the name next to `"documentation_api"` with a comment `# On-demand full tool docs (progressive descriptions)`).

Then append at the end of the file:

```python
# ── tool_docs + progressive descriptions ────────────────────────────────────────


class TestToolDocs:
    def test_returns_full_docstring(self):
        mod = _import_mod()
        result = _run(mod.tool_docs("query"))
        assert result["tool"] == "query"
        assert "Args:" in result["docs"]

    def test_unknown_tool_lists_valid_names(self):
        mod = _import_mod()
        with pytest.raises(ValueError, match="Valid tool names"):
            _run(mod.tool_docs("nope"))


class TestWireDescriptions:
    def _pristine(self):
        import importlib

        import app.api.mcp_http as mod

        return importlib.reload(mod)

    def test_all_descriptions_stay_short(self):
        # Ratchet: tools/list must never re-bloat. 500 chars per description.
        mod = self._pristine()
        for t in mod.mcp._tool_manager.list_tools():
            assert t.description, f"{t.name} has no description"
            assert len(t.description) <= 500, (
                f"{t.name}: {len(t.description)} chars (>500) — trim the "
                f"docstring's first paragraph"
            )

    def test_query_description_points_to_tool_docs(self):
        mod = self._pristine()
        t = mod.mcp._tool_manager.get_tool("query")
        assert "tool_docs('query')" in t.description
        assert "Args:" not in t.description  # detail moved off the wire
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_mcp_http.py -q -k "ToolDocs or WireDescriptions or exact_server_side"`
Expected: FAIL — `tool_docs` not registered, descriptions still full docstrings.

- [ ] **Step 3: Adopt the decorator in foundation_tools.py**

In `app/api/mcp/foundation_tools.py`:

1. Add the import next to the existing ones:

```python
from src.mcp_tooling import progressive_tool
```

2. After the `DATA_APP_TOOL_NAMES` tuple, add:

```python
# Full docstrings by tool name — the wire description carries only the first
# paragraph; the rest is served on demand by the `tool_docs` tool.
TOOL_DOCS: dict[str, str] = {}
```

3. Inside `register_foundation_tools`, immediately after its docstring:

```python
    tool = progressive_tool(mcp, TOOL_DOCS)
```

4. Swap all 59 decorators (macOS sed):

```bash
sed -i '' 's/^    @mcp\.tool()$/    @tool()/' app/api/mcp/foundation_tools.py
grep -c "@tool()" app/api/mcp/foundation_tools.py   # expect 59
grep -c "@mcp\.tool()" app/api/mcp/foundation_tools.py || true   # expect 0
```

5. Register the new tool at the end of `register_foundation_tools`, just before the `return list(FOUNDATION_TOOL_NAMES)`:

```python
    @tool()
    async def tool_docs(tool_name: str) -> dict:
        """Return the full reference documentation (docstring) for one registered MCP tool — arguments, return shape, and usage tips beyond the short description shown in the tool list."""
        doc = TOOL_DOCS.get(tool_name)
        if doc is None:
            known = ", ".join(sorted(TOOL_DOCS))
            raise ValueError(f"Unknown tool {tool_name!r}. Valid tool names: {known}")
        return {"tool": tool_name, "docs": doc}
```

(Single-paragraph docstring on purpose — the meta-tool must not point at itself.)

6. Add `"tool_docs",` to `FOUNDATION_TOOL_NAMES` right after `"documentation_api",`:

```python
    "documentation_api",
    # On-demand full tool docs — wire descriptions carry only the first
    # docstring paragraph; this returns the rest. MCP-surface-only (meta-tool
    # over the MCP tool registry itself; no REST/CLI analogue applies).
    "tool_docs",
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_mcp_http.py tests/test_mcp_tool_parity.py -q`
Expected: all PASS. If `test_all_descriptions_stay_short` fails on a specific tool, trim that tool's docstring **first paragraph** (move detail below the first blank line) until ≤500.

- [ ] **Step 5: Commit**

```bash
git add app/api/mcp/foundation_tools.py tests/test_mcp_http.py
git commit -m "feat(mcp): progressive tool descriptions + tool_docs on the foundation surface"
```

---

### Task 4: Server output guard on `query` + `describe`

**Files:**
- Modify: `app/api/mcp/foundation_tools.py` (`query`, `describe` returns)
- Modify: `tests/test_mcp_http.py` (guard tests)

**Interfaces:**
- Consumes: `ensure_output_size`, `MCPOutputTooLarge` from Task 1.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mcp_http.py` (reuse the file's existing `_mock_resp`, `_run`, `AsyncMock`, `patch` imports):

```python
from src.mcp_tooling import MCPOutputTooLarge


class TestOutputGuard:
    def _query(self, mod, resp_data):
        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            MC.return_value.__aenter__.return_value.post = AsyncMock(
                return_value=_mock_resp(resp_data)
            )
            return _run(mod.query("SELECT x FROM t"))

    def test_query_over_cap_raises_with_guidance(self, monkeypatch):
        mod = _import_mod()
        monkeypatch.setenv("AGNES_MCP_MAX_OUTPUT_CHARS", "1000")
        big = {"columns": ["x"], "rows": [["y" * 5000]], "truncated": False}
        with pytest.raises(MCPOutputTooLarge, match="output cap"):
            self._query(mod, big)

    def test_query_under_cap_passes(self, monkeypatch):
        mod = _import_mod()
        monkeypatch.setenv("AGNES_MCP_MAX_OUTPUT_CHARS", "1000")
        small = {"columns": ["x"], "rows": [[1]], "truncated": False}
        assert self._query(mod, small) == small

    def test_env_zero_disables_guard(self, monkeypatch):
        mod = _import_mod()
        monkeypatch.setenv("AGNES_MCP_MAX_OUTPUT_CHARS", "0")
        big = {"columns": ["x"], "rows": [["y" * 500_000]], "truncated": False}
        assert self._query(mod, big) == big

    def test_describe_over_cap_mentions_rows_hint(self, monkeypatch):
        mod = _import_mod()
        monkeypatch.setenv("AGNES_MCP_MAX_OUTPUT_CHARS", "500")
        wide = {"columns": [{"name": "x", "type": "VARCHAR", "blob": "z" * 5000}]}
        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            MC.return_value.__aenter__.return_value.get = AsyncMock(
                return_value=_mock_resp(wide)
            )
            with pytest.raises(MCPOutputTooLarge, match="rows"):
                _run(mod.describe("t1"))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_mcp_http.py -q -k OutputGuard`
Expected: FAIL — over-cap payloads are returned, not raised.

- [ ] **Step 3: Wrap the two returns**

In `app/api/mcp/foundation_tools.py` extend the import:

```python
from src.mcp_tooling import ensure_output_size, progressive_tool
```

In `query`, replace the line `return r.json()` (the existing `r.raise_for_status()` above it stays) with:

```python
            return ensure_output_size(r.json(), "query")
```

In `describe`, replace `return {"schema": rs.json(), "sample": rm.json()}` with:

```python
        return ensure_output_size(
            {"schema": rs.json(), "sample": rm.json()},
            "describe",
            hint="lower `rows` or select specific columns with the query tool",
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_mcp_http.py -q`
Expected: all PASS (including the pre-existing query/describe tests — small mocked payloads stay under the default cap).

- [ ] **Step 5: Commit**

```bash
git add app/api/mcp/foundation_tools.py tests/test_mcp_http.py
git commit -m "feat(mcp): output-size guard on foundation query/describe"
```

---

### Task 5: CLI stdio server adoption — decorators, `tool_docs`, guard

**Files:**
- Modify: `cli/mcp/server.py` (import, `TOOL_DOCS`, `tool = progressive_tool(...)`, 24× decorator swap, `tool_docs` tool, guard on `query`/`query_local`/`describe`)
- Modify: `tests/test_mcp_server.py` (sibling tests; fix any exact tool-set/count assertions)

**Interfaces:**
- Consumes: `progressive_tool`, `ensure_output_size`, `MCPOutputTooLarge` from Task 1.
- Produces: CLI `tool_docs(tool_name: str) -> dict` (sync), same contract as Task 3's.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mcp_server.py`:

```python
# ── tool_docs + progressive descriptions + output guard ─────────────────────


class TestToolDocs:
    def test_returns_full_docstring(self):
        srv = _import_server()
        result = srv.tool_docs("query")
        assert result["tool"] == "query"
        assert "Args:" in result["docs"]

    def test_unknown_tool_lists_valid_names(self):
        srv = _import_server()
        with pytest.raises(ValueError, match="Valid tool names"):
            srv.tool_docs("nope")


class TestWireDescriptions:
    def test_all_descriptions_stay_short(self):
        srv = _import_server()
        for t in srv.mcp._tool_manager.list_tools():
            assert t.description, f"{t.name} has no description"
            assert len(t.description) <= 500, (
                f"{t.name}: {len(t.description)} chars (>500) — trim the "
                f"docstring's first paragraph"
            )

    def test_query_description_points_to_tool_docs(self):
        srv = _import_server()
        t = srv.mcp._tool_manager.get_tool("query")
        assert "tool_docs('query')" in t.description


class TestOutputGuard:
    def test_query_over_cap_raises(self, monkeypatch):
        srv = _import_server()
        monkeypatch.setenv("AGNES_MCP_MAX_OUTPUT_CHARS", "1000")
        big = {"columns": ["x"], "rows": [["y" * 5000]], "truncated": False}
        with patch("cli.mcp.server.api_post_json", return_value=big):
            from src.mcp_tooling import MCPOutputTooLarge

            with pytest.raises(MCPOutputTooLarge, match="output cap"):
                srv.query("SELECT x FROM t")

    def test_query_under_cap_passes(self, monkeypatch):
        srv = _import_server()
        monkeypatch.setenv("AGNES_MCP_MAX_OUTPUT_CHARS", "1000")
        small = {"columns": ["x"], "rows": [[1]], "truncated": False}
        with patch("cli.mcp.server.api_post_json", return_value=small):
            assert srv.query("SELECT x FROM t") == small

    def test_query_local_over_cap_raises(self, monkeypatch, tmp_path):
        srv = _import_server()
        monkeypatch.setenv("AGNES_MCP_MAX_OUTPUT_CHARS", "500")
        monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path))
        db_dir = tmp_path / "user" / "duckdb"
        db_dir.mkdir(parents=True)
        import duckdb

        conn = duckdb.connect(str(db_dir / "analytics.duckdb"))
        conn.execute("CREATE TABLE t AS SELECT repeat('y', 5000) AS x")
        conn.close()
        from src.mcp_tooling import MCPOutputTooLarge

        with pytest.raises(MCPOutputTooLarge, match="output cap"):
            srv.query_local("SELECT x FROM t")
```

(If `TestQueryLocal` in this file builds its local DuckDB differently — e.g. a helper or different path layout — mirror THAT pattern instead; the assertion is what matters.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_mcp_server.py -q -k "ToolDocs or WireDescriptions or OutputGuard"`
Expected: FAIL — no `tool_docs` attribute, full-docstring descriptions, no guard.

- [ ] **Step 3: Adopt in cli/mcp/server.py**

1. Import next to the existing `src.duckdb_conn` import:

```python
from src.mcp_tooling import ensure_output_size, progressive_tool
```

2. Right after the `mcp = FastMCP("Agnes", instructions=...)` block:

```python
# Full docstrings by tool name — wire descriptions carry only the first
# paragraph; `tool_docs` serves the rest on demand.
TOOL_DOCS: dict[str, str] = {}
tool = progressive_tool(mcp, TOOL_DOCS)
```

3. Swap all 24 column-0 decorators:

```bash
sed -i '' 's/^@mcp\.tool()$/@tool()/' cli/mcp/server.py
grep -c "^@tool()" cli/mcp/server.py   # expect 24
```

4. Add the CLI `tool_docs` (sync) after the last existing tool definition:

```python
@tool()
def tool_docs(tool_name: str) -> dict:
    """Return the full reference documentation (docstring) for one registered MCP tool — arguments, return shape, and usage tips beyond the short description shown in the tool list."""
    doc = TOOL_DOCS.get(tool_name)
    if doc is None:
        known = ", ".join(sorted(TOOL_DOCS))
        raise ValueError(f"Unknown tool {tool_name!r}. Valid tool names: {known}")
    return {"tool": tool_name, "docs": doc}
```

5. Guard the three data tools:

In `query`, split the call from the return so `MCPOutputTooLarge` is not wrapped by the `V2ClientError` handler:

```python
    try:
        result = api_post_json("/api/query", {"sql": sql, "limit": limit})
    except V2ClientError as exc:
        raise ValueError(_mcp_error("query", exc)) from exc
    return ensure_output_size(result, "query")
```

In `describe`, same split:

```python
    rows = min(max(1, rows), 50)
    try:
        sch = api_get_json(f"/api/v2/schema/{table_id}")
        sam = api_get_json(f"/api/v2/sample/{table_id}", n=rows)
    except V2ClientError as exc:
        raise ValueError(_mcp_error(f"describe({table_id})", exc)) from exc
    return ensure_output_size(
        {"schema": sch, "sample": sam},
        "describe",
        hint="lower `rows` or select specific columns with the query tool",
    )
```

In `query_local`, wrap the final return:

```python
    return ensure_output_size(
        {
            "columns": columns,
            "rows": [list(r) for r in rows],
            "row_count": len(rows),
            "truncated": len(rows) == limit,
        },
        "query_local",
    )
```

- [ ] **Step 4: Run the whole CLI MCP test file; fix set/count assertions**

Run: `.venv/bin/pytest tests/test_mcp_server.py tests/test_mcp_tool_parity.py -q`
Expected failures to fix mechanically: any assertion enumerating the stdio server's tool names or count (e.g. the protocol smoke test `test_server_starts_and_lists_tools`) — add `tool_docs` to the expected set / bump the count. Re-run until green.

- [ ] **Step 5: Commit**

```bash
git add cli/mcp/server.py tests/test_mcp_server.py
git commit -m "feat(mcp): progressive descriptions, tool_docs, and output guard on the stdio server"
```

---

### Task 6: CHANGELOG, verify, draft PR

**Files:**
- Modify: `CHANGELOG.md` (`## [Unreleased]`)

- [ ] **Step 1: CHANGELOG bullets**

Under `## [Unreleased]` (create the section if absent), grouped per repo convention:

```markdown
### Added
- MCP: new `tool_docs(tool_name)` tool on both MCP surfaces (HTTP foundation + CLI stdio) returning a tool's full reference documentation on demand.

### Changed
- MCP: tool descriptions in `tools/list` now carry only the docstring's first paragraph plus a `tool_docs` pointer — the listing drops from ~9.6k to ~2k tokens; full docs moved behind `tool_docs`. A test ratchet caps every wire description at 500 chars.
- MCP: `query` and `describe` (plus CLI `query_local`) refuse responses whose serialized size exceeds `AGNES_MCP_MAX_OUTPUT_CHARS` (default 100 000; `0` disables) with actionable narrowing guidance, instead of returning megabyte payloads into the model's context.
```

- [ ] **Step 2: Deterministic verification**

```bash
.venv/bin/python scripts/verify_syncmap.py
.venv/bin/pytest tests/test_mcp_tooling.py tests/test_mcp_http.py tests/test_mcp_server.py tests/test_mcp_tool_parity.py tests/test_documentation_api_triple_surface.py -q
```

Expected: all green. (Full suite runs in CI — do not run it locally.)

- [ ] **Step 3: Commit, push, draft PR**

```bash
git add CHANGELOG.md
git commit -m "docs: changelog for MCP progressive docs + output guard"
git push -u origin HEAD:refs/heads/zs/airbyte-agent-engine-c6df40
gh pr create --draft --base main --head zs/airbyte-agent-engine-c6df40 \
  --title "feat(mcp): progressive tool docs + output-size guard" \
  --body "$(cat <<'EOF'
Two context-budget improvements to both MCP surfaces (HTTP foundation + CLI stdio), per docs/superpowers/specs/2026-08-03-mcp-progressive-docs-and-output-guard-design.md:

- **Progressive tool docs** — `tools/list` descriptions now carry only each docstring's first paragraph plus a pointer to the new `tool_docs(tool_name)` tool, which returns the full reference on demand. Listing weight drops ~9.6k → ~2k tokens; a test ratchet caps wire descriptions at 500 chars so the surface cannot re-bloat.
- **Output-size guard** — `query`/`describe` (and CLI `query_local`) raise an actionable "narrow the request" error when the serialized response exceeds `AGNES_MCP_MAX_OUTPUT_CHARS` (default 100 000; `0` disables) instead of returning megabyte payloads into the model's context. No partial data — an agent never computes over a silently-incomplete result. Row-level `limit`/`truncated` semantics are unchanged.

Docstrings in source stay full; only the wire description changes. No REST or CLI-command changes.
EOF
)"
gh pr checks --watch
```

- [ ] **Step 4: Watch CI, fix from logs**

`gh pr checks <n> --watch`; fix any failures from CI logs (that is the full-suite gate). Then hand off to the review loop (`/agnes-review` → fixes → Devin) per repo process.

---

## Self-Review Notes

- Spec coverage: Part A (guard, scope table incl. CLI `describe`) → Tasks 4–5; Part B (summary, pointer, `tool_docs`, ratchet) → Tasks 2–3, 5; shared module + env override → Task 1; CHANGELOG/rollout → Task 6. Release-cut decision (patch vs minor) is deliberately deferred to the pre-merge releaser step, per repo process.
- `MCPOutputTooLarge` subclasses `ValueError`, matching the CLI server's existing error convention; FastMCP converts any raised exception into an MCP error result the model sees.
- The guard reads env at call time, so `monkeypatch.setenv` works without reloads.
- `tool_docs` is MCP-surface-only; the triple-surface ratchet is REST-endpoint-driven, so no `_COHORT`/`_EXEMPT` entry is needed.
