# MCP progressive tool docs + output-size guard

**Date:** 2026-08-03
**Status:** approved
**Scope:** one PR, two independent but co-reviewed parts

## Motivation

Two context-efficiency gaps in the Agnes MCP surface, both patterns proven in
the wild by agent-tooling SDKs (notably the output-cap + docs-on-demand flow
in Airbyte's agent SDK):

1. **Tool-listing weight.** The server MCP surface registers 59 foundation
   tools whose full docstrings ship as tool descriptions on every
   `tools/list` — ~38.5k chars (~9.6k tokens) loaded into every MCP client
   session before the first tool call. Most of that text is cold reference
   (return shapes, edge-case notes) the model needs only when it actually
   calls the tool.

2. **Unbounded tool output.** The `query`/`describe` tools cap *rows*
   (default 1000, server-clamped) and set a `truncated` flag, but nothing
   caps *bytes*. 1000 rows of wide columns (JSON blobs, long text) can put
   megabytes into the agent's context with no warning and no recovery
   guidance. Silent giant payloads degrade every subsequent turn.

## Part A — output-size guard on MCP data tools

### Behavior

When the serialized JSON of a tool result exceeds the cap, the tool raises an
error instead of returning data. **No partial payload** — an agent must never
compute over silently-incomplete query results. The error message is
actionable, e.g.:

> query response ~340 KB exceeds the 100 KB output cap. Narrow the query:
> select specific columns, add a WHERE filter, lower `limit`, or aggregate
> server-side.

FastMCP converts the raised exception into an MCP error result; the model
sees the message and retries with a narrower request.

### Scope

| Server | Guarded tools |
|---|---|
| `app/api/mcp/foundation_tools.py` (both transports) | `query`, `describe` |
| `cli/mcp/server.py` (stdio) | `query`, `query_local`, `describe` |

Not guarded: `knowledge_search` / `collections_search` (bounded by `k`),
`agent_ask` (long text is the point), `documentation_api` (whole-file return
is the contract), and all mutation/admin tools (small responses by design).

### Configuration

- Default cap: **100 000 chars** of serialized JSON.
- Env override: `AGNES_MCP_MAX_OUTPUT_CHARS` (int; `0` disables the guard).
- No admin-UI / server-config knob (YAGNI; env is enough for operators).

### Interaction with existing limits

The row-level `limit` param and `truncated` flag are unchanged. The byte
guard is an orthogonal layer: row limit bounds cardinality, byte cap bounds
width × cardinality.

## Part B — progressive tool docs (auto-summary + `tool_docs`)

### Mechanism

At registration time, a wrapping decorator:

1. Takes the tool function's docstring.
2. Sends only the **first paragraph** as the MCP `description`.
3. If the docstring has more content, appends a pointer:
   `Full contract: tool_docs('<name>').`
4. Stores the full docstring in a module-level registry.

Docstrings in the source are untouched — they keep serving code readers and
the on-demand path. Parameter names/types/defaults still reach clients via
the JSON schema FastMCP derives from type hints, so the summary loses only
prose, not the signature.

### New foundation tool

`tool_docs(tool_name: str) -> dict` — returns
`{"tool": "<name>", "docs": "<full docstring>"}`. Unknown name → error
listing valid tool names (command-UX rule: a not-found error must hint the
next step). Added to `FOUNDATION_TOOL_NAMES`
and mirrored on the CLI MCP server (same helper, per-server registry).

### Ratchet

A test asserts every registered wire description is ≤ 500 chars, so future
tools cannot re-bloat `tools/list`. Expected effect on the server surface:
~9.6k → ~2k tokens.

## Shared implementation

New module `src/mcp_tooling.py` (importable from both `app/` and `cli/` —
precedent: `cli/mcp/server.py` already imports `src.duckdb_conn`):

- `summarize_docstring(doc) -> tuple[summary, has_more]`
- a decorator factory wrapping `FastMCP.tool()` that applies the summary +
  registers full docs
- `ensure_output_size(payload, tool_name, cap) -> payload` raising
  `MCPOutputTooLarge` with the actionable message
- cap resolution from env

## Testing (TDD)

- `tests/test_mcp_tooling.py` — unit: first-paragraph extraction (single- and
  multi-paragraph, multi-line first paragraph, missing docstring fallback),
  pointer appended only when more content exists, guard fires over cap /
  passes under cap / disabled at `0`.
- Server-side: `tool_docs` present in `FOUNDATION_TOOL_NAMES`;
  `tool_docs('query')` returns the full docstring (contains `Args:`);
  wire-description ratchet over all registered tools; `query` guard raises
  on an oversized mocked response.
- CLI-side siblings for the mirrored behavior.
- Existing `tests/test_mcp_tool_parity.py` stays green.

## Non-goals

- No REST API or `agnes query` CLI-command changes — terminal output does not
  bloat model context; REST callers page themselves.
- No per-call cap override parameter on tools.
- No Airbyte-style consolidation of tool families into execute meta-tools
  (would break every existing client and the REST×CLI×MCP parity contract).

## Rollout

Spec → implementation plan → TDD implementation → `verify-agnes-change` →
draft PR (CI runs full suite) → review loop. Release-cut: new `tool_docs`
tool + changed MCP wire descriptions = candidate for a **minor** version
bump; decide at release-cut time.
