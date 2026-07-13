# Command & search UX conventions (CLI × MCP × REST × web)

The user-facing command surface follows ONE mental model everywhere. This
playbook is the contract for any new command, MCP tool, search box, or flag —
and for any edit to an existing one. The sync-map rows "New CLI
search/discovery command" and "New MCP foundation tool" in `CONTRIBUTING.md`
point here; `/agnes-review` enforces them.

## The scope model

Every command/tool that *reads or finds* data answers "where does it look" the
same way:

1. **Default = everywhere / auto.** Search all sources; route queries to
   wherever the table lives. The user must never need to know in advance
   whether something is local, server-side, or remote (BigQuery).
2. **Origin is always labeled.** Results say where they came from — the
   `sources:` trailer on `agnes search`, the `[scope]` stderr note on
   `agnes query` auto-fallback, the type badge in the web global search,
   `source: "local"` + `note` in MCP offline fallbacks.
3. **Override is `--scope`.** Canonical values: `auto | local | server`.
   `--remote` and `--local` are frozen legacy shorthands — never introduce a
   NEW boolean scope flag, and never flip the default polarity of an existing
   command.
4. **Silent partial scope is forbidden.** If a run covers fewer sources than
   the default (offline mode, one marketplace tab, a client-side filter over a
   paginated list), the output must say so explicitly.

Reference implementations: `cli/commands/query.py` (`--scope auto` with
local→server fallback), `cli/commands/search.py` (source labeling + offline
warning), `app/web/static/js/global_search.js` (origin badges).

## Flag vocabulary

New commands MUST use the canonical column. Legacy aliases stay where they
already exist but are never added to new commands.

| Concept | Canonical | Frozen legacy aliases |
|---|---|---|
| search term | positional argument | `-q` / `--query` (option form) |
| result count | `--limit` | `--k` (search, collections search) |
| machine output | `--json` | — |
| execution/search scope | `--scope auto\|local\|server` | `--remote`, `--local` |

Dependent flags must not be silently ignored — either make the implied flag
implicit (`catalog --show` implies `--metrics`) or error loudly.

## Error hints ("not found" must point forward)

A discovery/query command that fails to resolve a name must tell the user the
next correct step, not just echo the server error:

- Local table miss → the shared helper `cli/query_hints.py`
  (`missing_table()` + `remote_table_hint()`). Both `agnes query` and the
  stdio MCP `query_local` tool use it — do not re-derive the regex or the
  wording.
- Registry 404 (`schema` / `describe`) → hint at `agnes catalog` and
  `agnes search`.
- MCP tools mirror the same hints with tool-appropriate wording
  (`surface="mcp"` points at the auto-routing `query` tool).

## MCP tool parity (one definition, every transport)

- Server-side foundation tools are defined ONCE in
  `app/api/mcp/foundation_tools.py` (`register_foundation_tools(...)`). Add
  the tool there and append its name to `FOUNDATION_TOOL_NAMES` — never
  hand-add a tool to `app/api/mcp_http.py` or `app/api/mcp_streamable.py`
  directly. `tests/test_mcp_tool_parity.py` fails if any transport misses a
  foundation tool.
- The stdio server (`cli/mcp/server.py`) is intentionally separate (offline
  filesystem access). When a tool exists on both sides, cross-link the
  docstrings and keep parameter names/defaults identical.
- Tool docstrings are the agent-facing UX: state the default scope, the
  routing behavior, and the sibling tool to use when scope is wrong.

## Web search surfaces

- Global cross-source search = the header box backed by
  `GET /api/knowledge/search` (documents + knowledge + catalog, RBAC-filtered).
  New "find anything" needs extend that endpoint rather than adding parallel
  search endpoints.
- A page-local search box must either search the full server-side set or
  visibly state its narrower scope. Scope selectors default to
  everything-checked (see marketplace).

## Checklist for review

- [ ] New read/find command defaults to auto/everywhere; `--scope` is the only
      new scope flag.
- [ ] Output labels result origin; degraded scope prints a warning.
- [ ] Flags: positional term, `--limit`, `--json`; no new `--k`/`--q`/boolean
      scope flags.
- [ ] "Not found" paths hint the next step (shared helpers where they exist).
- [ ] MCP: foundation tool added via `foundation_tools.py` +
      `FOUNDATION_TOOL_NAMES`; parity test green; docstring documents scope.
