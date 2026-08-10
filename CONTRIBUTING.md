# Contributing to Agnes

This file is the single source of truth for change-safety invariants. The
`/agnes-review` review team walks the sync-map below; human contributors should
too. Full design: `docs/superpowers/specs/2026-06-05-agnes-dev-agent-kit-design.md`.

## Dev workflow

1. Work on a branch (or an isolated git worktree).
2. TDD: write the failing test first, then the minimal implementation.
3. Keep changes vendor-agnostic — this is the public OSS distribution. No
   customer-specific deployments, project IDs, internal hostnames, or
   cross-references to private repos in code, config, comments, docs, or commits.
4. Run the full suite before pushing: `.venv/bin/pytest tests/ --tb=short -n auto -q`.
5. Add a `## [Unreleased]` CHANGELOG bullet for any user-visible behavior change.

## Verification loop

Before claiming a change is done, run the checks cheapest-first and fix what
fails until each passes:

```bash
python3 scripts/verify_syncmap.py          # instant, no venv — the sync-map rows below
.venv/bin/pytest tests/ --tb=short -n auto -q
/agnes-review                              # judgment only, once the above are green
```

`scripts/verify_syncmap.py` covers the sync-map rows that have no test guard.
The ordering is the point: anything a script can decide should never cost an LLM
reviewer a finding. The step-by-step loop (which guards to run for which diff,
how to treat WARN findings, when to add a new check) is
`.claude/skills/verify-agnes-change/SKILL.md`.

## Sync-map

Surfaces that must change together — and that CI does **not** fully guard. When
you touch the left column, update the middle column **in the same change**. Each
review finding cites two `file:line`: where the change landed, and where the
mirror is missing.

| Change | Mirror surface that MUST update | Severity | CI guard? |
|---|---|---|---|
| Method in `src/repositories/X.py` | sibling in `src/repositories/X_pg.py` | BLOCKING | partial |
| New repo class (either backend) | dispatch entry in `src/repositories/__init__.py` factory table (symmetric across backends) | BLOCKING | `tests/test_repository_registry.py` (static) |
| New callsite reading app-state | go through a `*_repo()` factory fn — never direct repo instantiation or raw `get_system_db()` | BLOCKING | `tests/test_backend_split_guard.py` (static) + `tests/db_pg/_parity_sweep_util.py` (dynamic) |
| New repo method | extend the matching `tests/db_pg/test_<cluster>_contract.py` | BLOCKING | partial |
| Alembic migration (PG) | matching `_vN_to_v(N+1)` in `src/db.py`; both ladders reach the same `SCHEMA_VERSION` | BLOCKING | `tests/test_db_schema_version.py` |
| New `ResourceType` enum value | `ResourceTypeSpec` in `app/resource_types.py` `RESOURCE_TYPES` | BLOCKING | `scripts/verify_syncmap.py` (full sweep) |
| New entity-scoped endpoint | `Depends(require_admin)` or `require_resource_access(...)` from `app/auth/access.py` | BLOCKING | `tests/test_route_auth_guard.py` (proves *some* auth) + `scripts/verify_syncmap.py` (WARN on authn-only entity routes) |
| New REST `/api/*` endpoint | a CLI command + an MCP tool that reach it (see "API coverage" below) | BLOCKING | `tests/test_documentation_api_triple_surface.py` (triple-surface ratchet) + `tests/test_api_docs_coverage.py` (docs) |
| User-visible behavior change | `## [Unreleased]` bullet in `CHANGELOG.md` | BLOCKING | `scripts/verify_syncmap.py` (skipped on a release-cut) |
| New connector extractor | `_meta` table contract (`table_name, description, rows, size_bytes, extracted_at, query_mode`); see `connectors/keboola/extractor.py` as canonical example | BLOCKING | partial |
| `query_mode='remote'` table | `_remote_attach` row in `extract.duckdb` | BLOCKING | `scripts/verify_syncmap.py` (connector must mention `_remote_attach`) |
| New web page | extends `base_ds.html` / `base_page.html` (never `base.html`); CSS in `head_extra` | BLOCKING | `tests/test_design_system_contract.py` (partial) |
| New/changed CLI or MCP read/find command | command-UX standard (`.claude/skills/agnes-conventions/references/command-ux.md`): default scope = auto/everywhere, origin labeled, `--scope` (never a new boolean scope flag), positional term + `--limit` + `--json`, "not found" hints the next step | BLOCKING | `scripts/verify_syncmap.py` (new boolean scope flag only — the rest is review) |
| New MCP foundation tool | defined in `app/api/mcp/foundation_tools.py` + name appended to `FOUNDATION_TOOL_NAMES` — never hand-added to a single transport module | BLOCKING | `tests/test_mcp_tool_parity.py` |
| New user-visible switch (feature flag, theme, layout, mode) | an entry in `app.switches.SWITCHES` + a row in `docs/feature-flags.md` (see that doc's "How to add a switch") — never a hand-rolled `os.environ.get(...)` / `get_value(...)` pair | BLOCKING | `tests/test_switches.py` (registry integrity) + `tests/test_admin_configure_api.py` (editable-section derivation) |
| PR landing the only `[Unreleased]` content | release-cut commit (version bump + CHANGELOG rename) in the same merge | per release rules | NO |
| Prompt rule edited in `app/initial_workspace_default/CLAUDE.md` (chat-sandbox bundled fallback) | mirror the same section in `config/claude_md_template.txt` (server-rendered default — what `WorkdirManager.run_init` overwrites it with on the common path, and what `agnes init` writes via `GET /api/welcome`) | BLOCKING | `tests/test_chat_answer_provenance_and_charts.py::test_the_bundled_and_server_default_sections_do_not_drift` |

### Parity enforcement reality

Parity is not just `X.py` ↔ `X_pg.py`. Backend selection lives in
`src/repositories/__init__.py` (a `{backend: (module, class)}` dispatch table
keyed off `use_pg()` / `DATABASE_URL`); callsites import `*_repo()` factory
functions, not repo classes. Two guards back the sync-map:

- **Static:** `tests/test_backend_split_guard.py` scans for direct repo
  instantiation + `get_system_db()` callers.
- **Dynamic:** `tests/db_pg/_parity_sweep_util.py` drives both backends through a
  `TestClient` and diffs the HTTP status of every parameter-free route.

The parity reviewer flags exactly what these guards cannot see.

### API coverage (REST × CLI × MCP)

Every new REST `/api/*` endpoint — except health checks, webhooks, OAuth
callbacks, and internal/SSE routes — must also be:

> **Standing exemption — admin credential-provisioning writes.** Endpoints
> whose request body carries or reconfigures upstream credential trust (vault
> secret writes, OAuth client registration/config) are CLI-reachable but
> deliberately **never** MCP-exposed: an agent-invokable tool that can
> re-point which upstream a credential authenticates against is a
> privilege-escalation seam, not a convenience. Classify them `_EXEMPT` with
> a pointer to this paragraph.

- **CLI-reachable:** a command under `cli/commands/` that calls the endpoint over
  HTTP via `cli/client.py`. State-changing endpoints also get a parity case in
  `tests/test_cli_api_parity.py`.
- **MCP-exposed:** either a static `@mcp.tool()` in `cli/mcp/server.py` that calls
  the endpoint, or a `tool_registry` passthrough row registered by
  `app/api/mcp/tools_generator.py`.

Refresh the endpoint inventory with `make update-openapi-snapshot` (generated by
`scripts/generate_openapi.py` into `tests/snapshots/openapi.json`).

**Enforcement reality:** structurally gated. `tests/test_api_docs_coverage.py`
fails if a public `/api/*` endpoint is undocumented;
`tests/test_documentation_api_triple_surface.py` is a ratchet that fails if a NEW
endpoint is neither classified as triple-surface (`_COHORT`, CLI + MCP verified)
nor consciously REST-only (`_EXEMPT`). Existing endpoints are grandfathered. The
review check below catches wiring *quality* the gates can't see (e.g. a CLI
command that exists but calls the wrong endpoint).
