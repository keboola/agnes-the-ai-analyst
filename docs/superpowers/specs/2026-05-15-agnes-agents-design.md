# Agnes specialized agents — design

**Status:** approved (brainstorm), not yet implemented
**Date:** 2026-05-15
**Author:** zsrotyr

## Problem

Working on Agnes through Claude Code, three classes of friction recur:

1. **Mental model drift.** Claude (and humans) forget how Agnes hangs together — the `extract.duckdb` contract, RBAC layering (`require_admin` vs `require_resource_access`), `query_mode` semantics (local / remote / materialized), the orchestrator's `rebuild()` flow. Edits to one part silently break invariants of another, caught only at code review.
2. **Convention enforcement at review.** Several CLAUDE.md rules (CHANGELOG bullet, vendor-agnostic OSS, no AI attribution, issue economy, RBAC gates on new endpoints) are easy to forget. Manual review catches most but not all.
3. **Release-cut workflow.** The non-negotiable rules from `docs/RELEASING.md` and `CLAUDE.md § Release process` — release-cut belongs in the PR that earned it, last commit on the PR, post-merge tag + GitHub Release — evolve and are repetitive enough that doing them by hand each time is error-prone.

The brainstorm explored variants A ("one big architect"), B ("three specialist subagents covering all three concerns"), and C ("knowledge as skills, enforcement/workflow as subagents"). C was selected because the mental model is *not delegatable* — when the main agent writes code it needs the context in its own window, not in a subagent's.

## Approach

Four layers, each with a distinct mechanism and lifecycle:

```
Layer 1: KNOWLEDGE SKILLS       (.claude/skills/agnes-*.md)
  - Auto-trigger by description + explicit invocation by main agent
  - Load into MAIN agent's context
  - Purpose: main agent knows how Agnes works while writing code

Layer 2: REVIEWER SUBAGENTS     (.claude/agents/agnes-reviewer-*.md)
  - Spawned via Agent tool at end of PR work
  - Own context window; return a punch list
  - Three specialists, fired in parallel by main agent

Layer 3: RELEASER SUBAGENT      (.claude/agents/agnes-releaser.md)
  - Spawned via Agent tool pre-merge (phase 1) and post-merge (phase 2)
  - Own context window; produces release-cut commit + tag + GitHub Release

Layer 4: PERSONAL               (~/.claude/agents/keboola-deploy.md)
  - Outside this repo
  - Customer-specific context (Keboola/Groupon VMs, gcloud accounts, infra repos)
```

Skills (Layer 1) share context with the main agent — when Claude edits
`src/orchestrator.py`, the orchestrator skill is loaded so rules stay "in head"
across delegation. Subagents (Layers 2 and 3) isolate context — review/release
read many files but only a punch list returns to the main conversation.

Cross-layer sharing happens through skills. The `agnes-release-process` skill is
read by the main agent (planning), by `agnes-reviewer-rules` (checking), and by
`agnes-releaser` (executing). Single source of truth.

## Components

### Layer 1 — Knowledge skills (`.claude/skills/`)

Four skills. Each file is ~80–120 lines. Files are kept focused; if one grows
past ~150 lines that is a signal to split. Skills do not duplicate `CLAUDE.md`
content verbatim — they reference (`see CLAUDE.md § Access control`) so master
rules have one location.

#### `agnes-orchestrator`

- **Description (triggers auto-spawn):** Use when editing `src/orchestrator.py`,
  `src/db.py`, or anything that produces `extract.duckdb` in `connectors/*/`.
  Rules for ATTACH flow, `query_mode` semantics, and when `rebuild()` is
  required.
- **Body:** master view lifecycle in `analytics.duckdb`; thread-safety via
  `_rebuild_lock`; `rebuild_source(name)` vs full `rebuild()` decision;
  `_remote_attach` reattach flow at query time (extension install + token
  resolution via `token_env` or extension-specific auth path).

#### `agnes-rbac`

- **Description:** Use when adding or changing an endpoint in `app/api/`,
  touching `app/auth/`, or introducing a new resource type. Enforces gate
  pattern (`require_admin` vs `require_resource_access`) and `ResourceType`
  registration.
- **Body:** decision tree for picking a gate (app-level mutation vs
  entity-scoped); how to add a new `ResourceType` (StrEnum value +
  `ResourceTypeSpec` with `list_blocks` delegate in `app/resource_types.py`, no
  DB migration); when a grant is needed even for reads; god-mode short-circuit
  for the `Admin` group.

#### `agnes-connectors`

- **Description:** Use when adding a new data source or modifying an existing
  extractor in `connectors/`. Enforces the `extract.duckdb` contract — `_meta`
  table, `query_mode` column, parquet layout.
- **Body:** required `_meta` columns (`table_name`, `description`, `rows`,
  `size_bytes`, `extracted_at`, `query_mode`); when a connector is batch-pull
  vs remote-attach vs real-time push; how to expose `_remote_attach` for remote
  mode; where the extractor writes (`/data/extracts/{source}/`).

#### `agnes-release-process`

- **Description:** Use before opening a PR, before merge, or when handling a
  release-cut. Rules for CHANGELOG bullet, when the release-cut commit belongs
  in the PR, version bump decision (patch is default; ask before minor).
- **Body:** CHANGELOG discipline (Added / Changed / Fixed / Removed / Internal
  grouping, `**BREAKING**` prefix); release-cut decision tree (when it is the
  last commit on the PR); post-merge sequence (tag `vX.Y.Z` on merge commit +
  `gh release create`); patch / minor / major guidance.

### Layer 2 — Reviewer subagents (`.claude/agents/`)

Each subagent has a standard frontmatter (`name`, `description`, `tools`,
`model`). All are read-only (no `Edit` / `Write`) — they return punch lists, not
code changes.

#### `agnes-reviewer-rules`

- **When fired:** every PR, at end of work, before opening the PR.
- **Tools:** `Read`, `Bash` (restricted to `git diff`, `git log`, `grep`).
- **Model:** Haiku — fast, mostly text work.
- **Input from main agent:** PR branch name (or current HEAD), optionally PR
  draft body.
- **Checks:**
  - CHANGELOG.md has a new bullet under `[Unreleased]` *iff* the PR changes
    user-visible behavior. Smart, not blind — doc-only PRs typically do not
    need a bullet; judgment applied based on the diff.
  - No customer-specific tokens in the diff or PR body (Keboola, Groupon,
    `keboola.com`, internal hostnames, GCP project IDs).
  - Commits do not contain `Co-Authored-By: Claude` or any AI attribution; PR
    body is the same.
  - Issue-economy red flags — filing follow-up issues rather than fix-it-now or
    close-it-as-moot.
  - Commit messages are clean and concise per project convention.
- Consults `agnes-release-process` skill for the release-cut implication of
  this PR.
- **Output:** Done / Missing / Warning punch list.

#### `agnes-reviewer-rbac`

- **When fired:** when the diff touches `app/api/`, `app/auth/`, or
  `app/resource_types.py`.
- **Tools:** `Read`, `Grep`, `Bash` (read-only).
- **Model:** Sonnet — needs to understand the auth flow.
- **Checks:**
  - New `@router.get/post/...` handlers have `Depends(require_admin)` or
    `Depends(require_resource_access(ResourceType.X, "..."))`.
  - New `ResourceType` values have a `ResourceTypeSpec` registration in
    `app/resource_types.py`.
- Consults `agnes-rbac` skill for the gate decision rules.
- **Output:** per-endpoint flag — gated correctly / missing gate / ambiguous.

#### `agnes-reviewer-architecture`

- **When fired:** when the diff touches `src/orchestrator.py`, `src/db.py`,
  `connectors/*/extractor.py`, or adds a schema migration.
- **Tools:** `Read`, `Grep`, `Bash` (read-only).
- **Model:** Sonnet.
- **Checks:**
  - Extractor changes preserve `_meta` table contract.
  - Remote-attach changes preserve `_remote_attach` columns (`alias`,
    `extension`, `url`, `token_env`).
  - Schema bumps in `src/db.py` include the `vN-1 → vN` migration step, a
    CHANGELOG note, and documentation references that reflect the new version.
  - Changes to `rebuild()` / `rebuild_source()` hold `_rebuild_lock` on all
    write paths.
- Consults `agnes-orchestrator` and `agnes-connectors` skills.
- **Output:** per-invariant punch list — holds / broken / unclear.

### Layer 3 — Releaser subagent (`.claude/agents/agnes-releaser.md`)

- **Tools:** `Read`, `Edit`, `Bash` (including `gh`, `git`).
- **Model:** Sonnet.

Two phases, each invoked explicitly by the user (never auto-fired).

**Phase 1 — pre-merge.** User says "ready to merge".

1. Consults `agnes-release-process` skill.
2. Runs `git log` since the last tag and inspects scope.
3. Decision tree: patch (default) vs minor (asks user) vs major (requires
   explicit confirmation).
4. If this PR lands the only `[Unreleased]` content since the last release,
   prepares the last commit on the PR: bump `pyproject.toml`, rename
   `[Unreleased]` to `[X.Y.Z] - YYYY-MM-DD`, add a new empty `[Unreleased]`.
5. Pushes the prepared commit. **Does not merge** — the user merges via
   `gh pr merge` themselves.

**Phase 2 — post-merge.** User says "tag it".

1. Verifies the merge commit contains the release-cut diff.
2. `git tag vX.Y.Z <merge-sha>` and `git push origin vX.Y.Z`.
3. `gh release create vX.Y.Z` with body extracted from the `[X.Y.Z]` section of
   CHANGELOG.
4. Returns the GitHub Release URL.

**Never does:** merges the PR (high-blast-radius); force-pushes; amends
published commits.

### Layer 4 — Personal (`~/.claude/agents/keboola-deploy.md`)

Outside this repo. Customer-specific content lives here so the OSS repo stays
vendor-agnostic per `CLAUDE.md § Vendor-agnostic OSS`.

- **Tools:** `Read`, `Bash` (including `gcloud`, `gh`, `git push`).
- **Model:** Sonnet.
- **Knows:**
  - VM ↔ project ↔ zone ↔ gcloud account mapping (Keboola
    `kids-ai-data-analysis europe-west1-b` with
    `--account=zdenek.srotyr@keboola.com`; Groupon
    `prj-grp-foundryai-dev-7c37 us-central1-a` with
    `--account=e_zsrotyr@groupon.com`).
  - Deploy ritual on `agnes-zsrotyr` = force-push to branch `zs/design-pass`.
  - Infra repo cross-references (`keboola/agnes-infra-keboola` vs
    `FoundryAI/agnes-the-ai-analyst-infra`), pinned module tag.
  - Default gcloud account is Groupon; Keboola commands need the explicit
    `--account=…` flag.
  - Pre-Agnes "Data Broker" legacy notes (separate zone `europe-north1-a`).
- **Does not** touch the OSS repo, push to public branches, or participate in
  PR review.

## End-to-end PR flow

```
1. User asks for feature X.

2. Main agent creates a worktree (per CLAUDE.local.md), invokes relevant
   knowledge skills based on what the feature touches:
   - src/orchestrator.py    → Skill(agnes-orchestrator)
   - app/api/ + app/auth/   → Skill(agnes-rbac)
   - connectors/            → Skill(agnes-connectors)

3. Implementation + tests + CHANGELOG bullet (the agnes-release-process skill
   reminded the main agent it is needed).

4. Before opening the PR, main agent fires reviewers in parallel — one message,
   multiple Agent tool calls:
   - Agent(agnes-reviewer-rules)         — always
   - Agent(agnes-reviewer-rbac)          — only if diff touched app/api or app/auth
   - Agent(agnes-reviewer-architecture)  — only if diff touched src/ or connectors/

5. Main agent aggregates the punch lists, fixes findings, opens the PR.

6. User says "ready to merge" → Agent(agnes-releaser, phase 1) prepares the
   release-cut decision and the last commit on the PR.

7. User confirms the version → releaser pushes the prepared commit. User
   merges via `gh pr merge` manually.

8. User says "tag it" → Agent(agnes-releaser, phase 2) creates the tag and the
   GitHub Release.
```

A separate flow handles personal dev-VM deploys (outside the OSS PR cycle):

```
User says "push to my VM" → main agent invokes Agent(keboola-deploy) →
keboola-deploy knows this means force-push to zs/design-pass, confirms with
the user (force-push is destructive), pushes.
```

## What lands in the repo

```
.claude/
├── agents/
│   ├── agnes-reviewer-rules.md
│   ├── agnes-reviewer-rbac.md
│   ├── agnes-reviewer-architecture.md
│   └── agnes-releaser.md
└── skills/
    ├── agnes-orchestrator.md
    ├── agnes-rbac.md
    ├── agnes-connectors.md
    └── agnes-release-process.md

docs/superpowers/specs/
└── 2026-05-15-agnes-agents-design.md   (this document)
```

`CLAUDE.md` gets a short paragraph under "Project conventions" pointing at
`.claude/agents/` and `.claude/skills/`, noting that subagents and skills exist
and how to invoke them.

The personal layer (`~/.claude/agents/keboola-deploy.md`) is written separately
and is not part of the in-repo change set.

## Non-goals

- **Not a Claude Code "team".** The architecture, review, and release agents
  work sequentially and do not message each other; a team would add the
  experimental flag, more tokens, file-conflict risk, and no functional gain.
  If `agnes-reviewer-*` ever needs to coordinate across four+ parallel reviews,
  re-evaluate.
- **Not a pre-commit hook replacement.** Mechanical checks (CHANGELOG bullet
  present, AI-attribution scan) could be a `pre-commit` hook in addition; the
  reviewer agent provides judgment-level checks the hook cannot.
- **No auto-merge.** `agnes-releaser` never runs `gh pr merge`. Merge is a
  visible, user-controlled action.

## Open questions

- **Auto-trigger reliability for knowledge skills.** Claude Code skill
  auto-spawn is description-driven and not always reliable in large skill
  catalogs. Mitigation: a one-line pointer at the top of `CLAUDE.md`
  ("when touching X, invoke skill Y") and explicit invocation in the main
  agent's planning step. If reliability is still low after a few weeks, fall
  back to a `SessionStart` hook that lists Agnes-specific skills as a reminder.
- **Schema migration as a separate skill?** `agnes-orchestrator` covers
  `src/db.py` migration patterns. If migration gotchas grow (versioning beyond
  `vN`, multi-step migrations, data backfills), split into a fifth skill
  `agnes-schema-migration`.

## Implementation plan

The follow-up step is to invoke the `writing-plans` skill to turn this spec
into a sequenced implementation plan covering: skills first (lowest risk,
testable in isolation), then reviewers, then the releaser, then a CLAUDE.md
pointer. Personal layer lands separately, outside the repo.
