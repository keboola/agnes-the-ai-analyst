# Agnes API Coverage Gate (Slice F) — Design

**Date:** 2026-06-08
**Status:** Approved design (reframed), pre-implementation
**Verified against:** `origin/main` 0.68.9 (branch rebased onto it)

## 0. Reframe note (supersedes the earlier YAML design)

The first draft of this spec proposed a standalone `tests/api_coverage.yaml`
ledger + baseline + a new `tests/test_api_coverage.py`. **That is superseded.**
While the branch was based on 0.66.1, PR **#565** landed in 0.68.0 and already
provides the coverage infrastructure:

- `tests/test_api_docs_coverage.py` — a **completeness ratchet**: every public
  `/api/*` path in `app.openapi()` must appear in `docs/api-reference.md` or match
  an `EXEMPT` prefix (with a reason). This is the "force every endpoint into a
  decision" pattern, for the docs surface.
- `tests/test_documentation_api_triple_surface.py` — a triple-surface check with a
  Python `_COHORT: dict[path, (cli_cmd, mcp_tool)]` + REST/CLI/MCP introspection
  (`app.web.router`, `cli.main.app.registered_groups`, `app.api.mcp_http`
  `mcp.list_tools()`). But it is **opt-in** — it only verifies the `_COHORT`.

An external YAML ledger would duplicate `_COHORT` and fight the existing Python
registry pattern (the architecture review flagged exactly this). So slice F is
**reframed**: extend the existing `_COHORT` test into a completeness ratchet,
mirroring `test_api_docs_coverage.py`'s shape. No new YAML, no parallel system.

## 1. Goal

Close the one genuine gap: #565's triple-surface check is opt-in (`_COHORT` has 1
entry). A new analyst-facing endpoint can ship without CLI/MCP and nothing fails.
Slice F makes every NEW `/api/*` endpoint require a conscious classification —
either **triple-surface** (in `_COHORT`, verified) or **REST-only** (exempt, with
a reason) — while grandfathering the ~200 existing endpoints (ratchet, not sweep).

## 2. Decisions (locked)

| Axis | Decision |
|---|---|
| Where | Extend `tests/test_documentation_api_triple_surface.py` — no new test module, no YAML. |
| Model | Ratchet — grandfather existing `/api/*`; force new endpoints into a decision. |
| Detection | The existing Python `_COHORT` registry (verified via #565's REST/CLI/MCP introspection). |
| Policy | Not every endpoint needs CLI+MCP — new endpoints must be **classified** (cohort or exempt), not necessarily covered. |

## 3. Architecture — extend the #565 test

Add three module-level structures + one test to
`tests/test_documentation_api_triple_surface.py`:

- **`_COHORT`** (exists) — endpoints that MUST be triple-surface: `path → (cli_cmd, mcp_tool)`. The 3 existing tests already verify these on REST/CLI/MCP.
- **`_EXEMPT`** (new) — `dict[path, reason]`: endpoints that are consciously REST-only (admin mutations, internal, webhooks). Reason required.
- **`_GRANDFATHERED`** (new) — the set of `/api/*` (and `/documentation/*`) paths that existed at slice-F landing, loaded from a committed `tests/api_triple_surface_grandfathered.txt` (one path per line, sorted). Exempt from classification *for now* — the ratchet baseline.
- **`test_new_endpoints_are_classified`** (new):
  ```
  live = {p for p in app.openapi()["paths"]
          if p.startswith(("/api/", "/documentation/"))}
  unclassified = live - set(_COHORT) - set(_EXEMPT) - _GRANDFATHERED
  assert not unclassified  # each new endpoint: add to _COHORT or _EXEMPT
  ```
  A new endpoint in none of the three sets → FAIL with guidance: add to `_COHORT`
  (and land the CLI + MCP surfaces) or `_EXEMPT` (with a reason).

The baseline `.txt` is generated once (all current `/api/*` + `/documentation/*`
minus the existing `_COHORT`). It only shrinks: covering a grandfathered endpoint
means moving it to `_COHORT` and deleting its baseline line (encouraged, not
required). A `.txt` (vs a ~200-entry Python literal) is the right home for that
much data; the test also asserts the baseline is a subset of live paths (no stale
lines linger → T2-style no-orphans).

**Guards against the empty-set trap** (a gate that guards nothing): assert
`len(live) > 150`, `_GRANDFATHERED` non-empty and `⊆ live`. The CLI/MCP
introspection reuses #565's existing (already-asserting) tests.

## 4. Seed (one-time) — `scripts/seed_triple_surface_baseline.py`

Computes `live` from `create_app().openapi()`, writes every `/api/*` +
`/documentation/*` path not already in `_COHORT` to
`tests/api_triple_surface_grandfathered.txt` (sorted). Run once, commit, then it
stays for re-seed audits. Never runs in CI.

## 5. Staleness reconciliation (folded into this slice)

The dev-agent kit (built on 0.66.1) now contains claims that are false on 0.68.9:

- `CONTRIBUTING.md` sync-map row "New REST `/api/*` endpoint" → **review-only (no
  structural gate yet)**, and the "API coverage" subsection's "Enforcement
  reality" note. → Update to: gated by `tests/test_api_docs_coverage.py` (docs)
  and `tests/test_documentation_api_triple_surface.py` (triple-surface ratchet,
  extended here). Review still catches wiring *quality* the gate can't see.
- `agnes-reviewer-rbac.md` "API coverage check" note: "There is no structural
  gate, so this review check is the floor." → reference the two gates as the
  structural floor; the reviewer covers correctness of the wiring.
- Restore the 6 dev-agent-kit `## [Unreleased]` CHANGELOG bullets dropped during
  the rebase, with the coverage one reworded to reflect the #565 gate (kit adds a
  *review layer + the completeness ratchet*, not "the first gate").

## 6. Non-goals (YAGNI)

- No exhaustive backfill — existing endpoints are grandfathered.
- No requirement that every endpoint be CLI+MCP — only that new ones are
  *classified*. Most stay REST-only via `_EXEMPT`/grandfather.
- No new YAML/registry — reuse `_COHORT` + #565 introspection.
- No change to `test_api_docs_coverage.py` (docs completeness already works).

## 7. Verified facts (origin/main 0.68.9)

- `_COHORT` shape + REST (`app.web.router`), CLI (`cli.main.app.registered_groups`/
  `registered_commands`), MCP (`app.api.mcp_http` → `asyncio.run(mcp.list_tools())`)
  introspection: all present in `tests/test_documentation_api_triple_surface.py`.
- `create_app().openapi()["paths"]` + `/api/` filtering: `tests/test_api_docs_coverage.py`.
- The kit's stale "review-only" claims: `CONTRIBUTING.md`, `.claude/agents/agnes-reviewer-rbac.md`.
