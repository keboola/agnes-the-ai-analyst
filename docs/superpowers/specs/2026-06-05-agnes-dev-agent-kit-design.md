# Agnes Dev-Agent Kit — Design

**Date:** 2026-06-05
**Status:** Approved design, pre-implementation
**Verified against:** `0.66.1` (HEAD `92ea180e`)

## 1. Context & goal

Agnes already ships a small set of Claude Code dev artifacts under `.claude/`:
four reviewer/releaser agents (`agnes-reviewer-{rules,rbac,architecture}`,
`agnes-releaser`) and four knowledge skills (`agnes-{orchestrator,connectors,rbac,release-process}`).
They are spawned ad-hoc; there is no orchestration, no slash command, no
quality hook, and the review invariants live as prose scattered across a large
`CLAUDE.md`.

The goal is a coherent **dev-agent kit** that turns this loose collection into a
disciplined, orchestrated system, following the thin-agent / fat-skill pattern
seen in mature CLI-embedded Claude Code tooling. Two capabilities anchor it:

- a **review team** that walks a declarative invariant table ("sync-map") and
  catches the silent-drift failure modes CI does not, and
- a **build team** that implements decomposed work in parallel git worktrees
  under the same invariants, then feeds the result back through review.

The kit stays in `.claude/` (auto-discovered; zero-friction for contributors of
this repo) — **not** packaged as a distributable marketplace plugin.

## 2. Inspiration (the CLI-embedded dev-agent pattern)

- **Thin agent / fat skill** — agents are short routers; knowledge lives in
  `SKILL.md` + `references/*.md`, loaded on demand.
- **Silent-drift hunt** — for every change, walk a `CONTRIBUTING.md` table of
  surfaces that must move together and that CI does not guard; cite **two**
  `file:line` per finding (where the change landed + where the mirror is missing).
- **Read-only / comment-only review discipline** — the reviewer never edits the
  working tree, never approves/requests-changes/merges; the human keeps every
  veto. Severity rubric (BLOCKING / NON-BLOCKING / NIT), default to NON-BLOCKING
  when unsure, `file:line` + severity mandatory, ≤15 findings, verify-don't-assume.
- **slash command + subagent split** — the command resolves context in the main
  conversation; the heavy playbook runs in a fresh subagent window so discipline
  survives long sessions.
- **PostToolUse quality hook** — auto-lint/format every edit, block on errors.
- **Real Team orchestration** — `TeamCreate` + `TaskCreate` with `addBlockedBy`
  dependencies + `SendMessage` + `TeamDelete`, visible in `/workflows`.

## 3. Decisions (locked during brainstorm)

| Axis | Decision |
|---|---|
| Scope | All four areas: review core, quality hook, builder agent, router + thin/fat refactor. **No** plugin packaging. |
| Review model | Scope-gated subset + consolidator, run as a **real Team** (not lightweight parallel spawns), with the **existing roster** (rules / architecture / rbac / parity). |
| Sync-map home | New `CONTRIBUTING.md` at repo root (serves humans + the reviewer agent). |
| Builder | Disciplined general feature-implementer (sub-workflows as skill references), not a narrow scaffolder. |
| Build orchestration | A **build team**: decomposer → parallel worktree implementers → integrator → auto-review. |
| Review action | Advisory, local-first (terminal report); optionally post one comment-only review when an open PR exists. Working tree read-only. |
| Packaging | `.claude/` only + a router section in `CLAUDE.md`. |

## 4. Architecture & file layout

```
CONTRIBUTING.md                          [NEW]  sync-map table + review playbook + dev workflow
CLAUDE.md                                [EDIT] + "Dev agents & commands — which to use when" router section
scripts/post-edit-quality.sh             [NEW]  ruff --fix/format (block) + mypy (warn)
.claude/
  settings.json                          [NEW]  PostToolUse hook wiring
  commands/
    agnes-review.md                      [NEW]  scope-gate → Team → consolidator → advisory report (+ optional PR comment)
    agnes-build.md                       [NEW]  decomposer → worktree implementers → integrator → /agnes-review
  agents/
    agnes-reviewer-rules.md              [KEEP+] reads sync-map from CONTRIBUTING.md
    agnes-reviewer-rbac.md               [KEEP+]
    agnes-reviewer-architecture.md       [KEEP+]
    agnes-reviewer-parity.md             [NEW]  DuckDB↔PG parity + factory + migration ladder + sync-map drift
    agnes-review-consolidator.md         [NEW]  merge findings → one report, dedup + severity escalation
    agnes-builder.md                     [NEW]  disciplined feature-implementer
    agnes-decomposer.md                  [NEW]  sync-map-aware split into independent tasks
    agnes-integrator.md                  [NEW]  merge worktree diffs, serialize migrations, fold merge-magnets
    agnes-releaser.md                    [KEEP]
  skills/
    agnes-orchestrator/  agnes-connectors/  agnes-rbac/  agnes-release-process/   [as-is until they grow]
    agnes-conventions/SKILL.md           [NEW]  non-negotiables + builder playbooks (references/*.md)
```

The four existing skills are already small (64–86 lines). The thin/fat split is
applied **only where content grows** — primarily the new `agnes-conventions`
skill, whose `references/` hold the builder's per-task playbooks so the builder
agent prompt stays thin. Existing skills are left flat until they bloat.

## 5. Sync-map (`CONTRIBUTING.md`) — the heart

A declarative table of surfaces that must change together and that CI does not
fully guard. Reviewers (rules + parity) walk it row by row.

| Change | Mirror surface that MUST update | Severity | CI guard? |
|---|---|---|---|
| Method in `src/repositories/X.py` | sibling in `src/repositories/X_pg.py` | BLOCKING | partial |
| New repo class (either backend) | dispatch entry in `src/repositories/__init__.py` factory table (symmetric across backends) | BLOCKING | `test_backend_split_guard.py` (static) |
| New callsite reading app-state | go through a `*_repo()` factory fn — never direct repo instantiation or raw `get_system_db()` | BLOCKING | `test_backend_split_guard.py` (static) + parity sweeps (dynamic) |
| New repo method | extend `tests/db_pg/test_<cluster>_contract.py` | BLOCKING | partial |
| Alembic migration (PG) | matching `_vN_to_v(N+1)` in `src/db.py`; both ladders reach the same `SCHEMA_VERSION` | BLOCKING | `test_db_schema_version.py` + `test_alembic_roundtrip.py` |
| New `ResourceType` enum value | `ResourceTypeSpec` in `app/resource_types.py` `RESOURCE_TYPES` | BLOCKING | NO |
| New entity-scoped endpoint | `Depends(require_admin)` or `require_resource_access(...)` from `app.auth.access` | BLOCKING | NO |
| User-visible behavior change | `## [Unreleased]` bullet in `CHANGELOG.md` (grouped, `**BREAKING**` prefix if needed) | BLOCKING | NO |
| New connector extractor | `_meta` table contract (`table_name, description, rows, size_bytes, extracted_at, query_mode`) via `_create_meta_table` pattern | BLOCKING | partial |
| `query_mode='remote'` table | `_remote_attach` row in `extract.duckdb` (`alias, extension, url, token_env`) | BLOCKING | NO |
| New web page | extends `base_ds.html` / `base_page.html` (never `base.html`); CSS in `head_extra`, not inline | BLOCKING | `test_design_system_contract.py` (partial) |
| PR landing the only `[Unreleased]` content | release-cut commit (version bump + CHANGELOG rename + new empty `[Unreleased]`) in the same merge | per release rules | NO |

**Enforcement reality (verified):** parity is not just `X.py ↔ X_pg.py`. The
repo factory in `src/repositories/__init__.py` selects the backend via a
`{backend: (module, class)}` table keyed off `use_pg()` / `DATABASE_URL`;
callsites import `*_repo()` factory functions, not classes. The
`test_backend_split_guard.py` static ratchet catches direct instantiation +
`get_system_db()` callers; the `_parity_sweep_util.py` dynamic sweeps diff HTTP
status per route across both backends. The parity reviewer leans on these
existing guards and flags exactly what they cannot see.

The sync-map is the single source of truth shared by the review team **and** the
build team's decomposer (coupling rules below derive from it).

## 6. Review team — `/agnes-review`

Real Team orchestration, scope-gated membership, advisory + optional PR comment.

```
1. base = git merge-base origin/main HEAD   (or explicit arg)
   changed = git diff --name-only <base>...HEAD
2. scope-gate → members that fire:
     rules        → always (CHANGELOG, vendor-agnostic, commit hygiene, issue economy)
     architecture → src/orchestrator.py, src/db.py, connectors/*/extractor.py, migrations
     rbac         → app/api/, app/auth/, app/resource_types.py
     parity       → src/repositories/*, src/db.py, migrations/, tests/db_pg/
3. TeamCreate("agnes-review")
   TaskCreate × in-scope reviewers (no deps)
   TaskCreate(consolidator, addBlockedBy=[…reviewer task ids])
   spawn in-scope reviewers (Agent, run_in_background, read-only tools: Read/Grep/Bash)
4. wait → consolidator merges (dedup, escalate severity on multi-reviewer overlap)
   → ONE report: verdict + BLOCKING/NON-BLOCKING/NIT, file:line each, ≤15 findings
5. output: print to terminal; if an open PR exists for the branch → optionally
   post one comment-only review via `gh pr review --comment --body-file`
   (never --approve / --request-changes / merge)
6. TeamDelete; working tree untouched — fixes happen in a follow-up step (main agent or builder)
```

**Discipline (per reviewer + consolidator):** `file:line` mandatory, severity
mandatory, ≤15 findings, verify-don't-assume (every verification-log line maps to
a real command run), default NON-BLOCKING when unsure, GitHub body in English /
summary to the user in the parent's language (Czech).

## 7. Build team — `/agnes-build`

Parallel implementation in isolated worktrees, under the same invariants. **Key
insight: the sync-map that drives review also drives safe decomposition** —
coupled surfaces must not be split across parallel tasks.

```
1. DECOMPOSER (agnes-decomposer)
   reads a writing-plans plan + the sync-map coupling rules
   → splits work into INDEPENDENT tasks; keeps coupled surfaces together:
       • parity siblings (X.py + X_pg.py + contract test)      → one task
       • one migration step (Alembic + db.py _vN_to_v)         → one task, and only ONE per run (ladder is serial)
       • ResourceType enum + Spec registration                 → one task
   → CHANGELOG.md and CLAUDE.md are "merge magnets": implementers DO NOT edit them;
     each emits its bullet in structured output for the integrator to fold in one pass
2. IMPLEMENTERS (= agnes-builder instances, one per task, isolation:'worktree', parallel)
   each holds the non-negotiables (TDD, in-task parity, scope discipline)
3. INTEGRATOR (agnes-integrator)
   collects worktree diffs; applies the migration task LAST (serialized);
   folds CHANGELOG/CLAUDE.md bullets; resolves residual conflicts
4. → auto /agnes-review on the unified diff
```

**Mechanism:** Team API (`TeamCreate` + `Agent` with `isolation:'worktree'` +
`run_in_background`), invocable from the command file, visible in `/workflows`.
For heavy ad-hoc runs the `Workflow` tool is an alternative the user can opt into,
but the packaged capability uses the Team API.

**Risks the design addresses:** parallel edits to the same file (worktree
isolation + merge-magnet rule), migration-ladder conflict (serialized migration
task), parity split in half (decomposer coupling rule).

This is the most complex piece and ships **last** (slice E), after the sync-map,
builder, and review core exist.

## 8. Builder agent contract (`agnes-builder`)

Non-negotiable rules (a disciplined-executor contract, in the spirit of an ops
specialist agent but adapted from "mutate a live external system" to "write code
in this repo"):

1. **TDD-first** — a failing test before implementation.
2. **Dual-backend parity in the SAME change** — touch `X.py` → `X_pg.py` +
   contract test; never "PG later". Reach repos via the factory.
3. **Migration ladder** — Alembic step ↔ `db.py` `_vN_to_v(N+1)`, both reach the
   same `SCHEMA_VERSION`.
4. **CHANGELOG bullet** for user-visible behavior.
5. **Vendor-agnostic** — no customer-specific tokens in code/config/comments/docs.
6. **Scope discipline + issue economy** — fix/close, don't sprawl or file noise.
7. **Web pages** via `base_ds.html` / `base_page.html`, never `base.html`.
8. **Run the full test suite before claiming done** (`.venv/bin/pytest tests/ --tb=short -n auto -q`).

**Output contract:** structured summary — what changed · parity sibling touched? ·
CHANGELOG bullet added? · tests run + result · next step.

**Playbooks** (in `agnes-conventions/references/`): new connector · new endpoint +
RBAC gate · new web page · new repo method + parity · schema migration.

**Tools:** Read, Write, Edit, Bash, Grep, Glob, TodoWrite.

## 9. Quality hook

`scripts/post-edit-quality.sh`, wired via `.claude/settings.json` PostToolUse
(matcher `Edit|Write|MultiEdit`, `.py` files only):

- `ruff check --fix` → **blocks** on unresolved issues (exit ≠ 0)
- `ruff format`
- `mypy --ignore-missing-imports` → **warning only** (mirrors CI's advisory
  posture; never blocks)
- **No pytest** in the hook (per-edit is too slow; the full suite stays a
  pre-push gate)

Reads the tool-input JSON from stdin (hook contract), extracts the edited path,
runs from the repo root so `pyproject.toml` config applies. Supports manual
invocation for debugging (echo a JSON payload into it).

## 10. Router (`CLAUDE.md` section)

A "Dev agents & commands — which to use when" table:

| Need | Use | How |
|---|---|---|
| Review a change before merge | `/agnes-review` | scope-gated Team + consolidator |
| Implement a decomposed plan in parallel | `/agnes-build` | decomposer → worktree implementers → integrator → review |
| Implement a single feature/connector/endpoint | `agnes-builder` | Agent, or follow its playbook inline |
| Cut a release / tag | `agnes-releaser` | per release process |
| Deep knowledge (orchestrator/rbac/connectors/release/conventions) | `agnes-*` skills | auto-loaded by description |

## 11. Testing strategy

- **Sync-map guard** (`tests/test_contributing_sync_map.py`, new): parse the
  `CONTRIBUTING.md` table and assert every referenced path/symbol still exists —
  protects the doc from its own drift.
- **Quality hook** (`tests/test_post_edit_quality.py`, new): drive
  `scripts/post-edit-quality.sh` via subprocess with a JSON stdin payload; assert
  ruff fix/format applied and mypy does not block.
- **Agents / commands**: no automated tests (they are prompts); validated by use.

## 12. Implementation slices

| Slice | Content | Depends on |
|---|---|---|
| **A** | Review core: `CONTRIBUTING.md` sync-map + `agnes-reviewer-parity` + `agnes-review-consolidator` + `/agnes-review` Team + sync-map guard test | — |
| **B** | Quality hook: `scripts/post-edit-quality.sh` + `.claude/settings.json` + hook test | — |
| **C** | Builder: `agnes-builder` + `agnes-conventions` skill + references | A (sync-map) |
| **D** | Router section in `CLAUDE.md` + thin/fat refactor where needed | A–C |
| **E** | Build team: `agnes-decomposer` + `agnes-integrator` + `/agnes-build` | A + C |

## 13. First dogfood

Once slice A lands, the **first real job** for `/agnes-review` (or a dedicated
doc-drift pass) is a documentation-accuracy audit of `README.md`, `CLAUDE.md`,
and `docs/` — dead links, superseded paths/names, stale version numbers. This is
exactly the silent-drift class the kit targets, and using the kit on it validates
the tooling.

## 14. Non-goals (YAGNI)

- No marketplace/plugin packaging (auto-discovery in `.claude/` is sufficient for
  this repo).
- No new reviewer specialists beyond the existing roster (security/perf/etc. can
  be added later if a need proves out).
- The build team does **not** attempt cross-task conflict resolution beyond the
  decomposer coupling rules + integrator serialization; genuinely entangled work
  is kept in one task rather than force-parallelized.

## 15. Verification appendix

Checked against `0.66.1` (HEAD `92ea180e`); all claims held:

- 41 `*_pg.py` + 40 non-`pg` repos; factory in `src/repositories/__init__.py`.
- `tests/db_pg/` contract tests + `tests/db_pg/_parity_sweep_util.py`; static guard at `tests/test_backend_split_guard.py`.
- `SCHEMA_VERSION = 72`; `_vN_to_v` ladder with `_finalize`/`_migrate` variants;
  `alembic.ini` + `test_alembic_roundtrip/skeleton/backfill`.
- `ResourceType` StrEnum + `ResourceTypeSpec` + `RESOURCE_TYPES` in `app/resource_types.py`.
- `require_admin` / `require_resource_access` in `app/auth/access.py`.
- `## [Unreleased]` in `CHANGELOG.md`.
- `_create_meta_table` in `connectors/keboola/extractor.py`; `_remote_attach`
  across connectors + orchestrator.
- `base.html` / `base_ds.html` / `base_page.html` + `test_design_system_contract.py`.
- `DUCKDB` / `CLOUD` / `SIDE_CAR` / `DUCKDB_QUACK` states in `src/db_state_machine.py`.
- Tooling: ruff (pre-commit + `make lint`) + mypy (advisory CI); pytest with
  xdist/split.

One inaccuracy found and fixed inline: the `CLAUDE.md` dual-backend section
named only the `X.py ↔ X_pg.py` rule; it now also documents the factory +
`test_backend_split_guard.py` + parity sweeps.
