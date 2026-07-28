# Agnes specialized agents — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the four-layer Agnes agent design from `docs/superpowers/specs/2026-05-15-agnes-agents-design.md` — four knowledge skills, three reviewer subagents, one releaser subagent, plus a CLAUDE.md pointer.

**Architecture:** Skills load into the main agent's context window (carrying mental model alongside the work). Subagents are spawned via the Agent tool — reviewers in parallel at end of PR, releaser explicitly pre-merge and post-merge. Cross-layer rule sharing flows through skills (e.g., both `agnes-reviewer-rules` and `agnes-releaser` consult `agnes-release-process`). Personal layer (`~/.claude/agents/<customer>-deploy.md`) stays out of the OSS repo — not part of this plan.

**Tech Stack:** Markdown with YAML frontmatter (Claude Code skill + agent format). No code, no tests-as-code — validation is YAML lint + smoke invocation.

---

## File Structure

| Path | Purpose |
|---|---|
| `.gitignore` | Un-ignore `.claude/agents/` and `.claude/skills/` (currently `.claude/` is fully ignored) |
| `.claude/skills/agnes-release-process.md` | Release-cut decision tree, CHANGELOG discipline |
| `.claude/skills/agnes-orchestrator.md` | extract.duckdb contract, ATTACH flow, query_mode semantics |
| `.claude/skills/agnes-rbac.md` | `require_admin` vs `require_resource_access`, ResourceType registration |
| `.claude/skills/agnes-connectors.md` | `_meta` table contract, `_remote_attach`, new-connector pattern |
| `.claude/agents/agnes-reviewer-rules.md` | Convention enforcement — CHANGELOG, vendor-agnostic, AI-attribution |
| `.claude/agents/agnes-reviewer-rbac.md` | Gate / ResourceType registration check |
| `.claude/agents/agnes-reviewer-architecture.md` | extract.duckdb / orchestrator / schema invariants |
| `.claude/agents/agnes-releaser.md` | Pre-merge release-cut + post-merge tag/Release |
| `CLAUDE.md` | Add short pointer to `.claude/agents/` and `.claude/skills/` |

All work happens in the worktree at `.claude/worktrees/zs+agnes-agents-spec` on branch `worktree-zs+agnes-agents-spec`.

---

## Validation primitives

Each task ends with a **YAML lint** + **smoke invocation**. Reused commands:

**YAML lint** (frontmatter parses, required keys present):

```bash
python3 -c "
import sys, yaml, pathlib
p = pathlib.Path(sys.argv[1])
text = p.read_text()
assert text.startswith('---\n'), f'{p}: missing opening ---'
end = text.find('\n---\n', 4)
assert end > 0, f'{p}: missing closing ---'
fm = yaml.safe_load(text[4:end])
required = sys.argv[2].split(',')
missing = [k for k in required if k not in fm]
assert not missing, f'{p}: missing keys {missing}'
print(f'OK {p}')
" <file> <required-keys>
```

**Smoke invocation:** open the file in this session via `Read`, scan for: (a) the frontmatter renders, (b) all section headings present per the plan, (c) no `TODO` / `TBD` / `XXX` in the body, (d) cross-references (`see CLAUDE.md § ...`, `Skill(agnes-...)`) match real targets.

---

## Task 0: Allow `.claude/agents/` and `.claude/skills/` in git

**Files:**
- Modify: `.gitignore`

- [ ] **Step 1: Inspect current `.gitignore`**

Run: `head -5 .gitignore`
Expected: opens with `# Claude Code` block ignoring `.claude/` entirely.

- [ ] **Step 2: Update `.gitignore` to un-ignore `agents/` and `skills/`**

Replace the `# Claude Code` block with:

```gitignore
# Claude Code
.claude/*
!.claude/agents/
!.claude/skills/
CLAUDE.local.md
```

- [ ] **Step 3: Verify `.claude/settings.local.json` and `.claude/worktrees/` are still ignored**

Run:
```bash
git check-ignore -v .claude/settings.local.json .claude/worktrees/foo .claude/agents/test.md .claude/skills/test.md
```

Expected:
- `settings.local.json` ignored
- `worktrees/foo` ignored
- `agents/test.md` NOT ignored (no output line for it)
- `skills/test.md` NOT ignored

- [ ] **Step 4: Create the directories**

```bash
mkdir -p .claude/agents .claude/skills
```

- [ ] **Step 5: Commit**

```bash
git add .gitignore
git commit -m "chore: un-ignore .claude/agents and .claude/skills"
```

---

## Task 1: Skill `agnes-release-process`

**Files:**
- Create: `.claude/skills/agnes-release-process.md`

Other reviewers and the releaser reference this skill, so it lands first.

- [ ] **Step 1: Write the file**

Path: `.claude/skills/agnes-release-process.md`

```markdown
---
name: agnes-release-process
description: Rules for opening a PR, the CHANGELOG bullet, the release-cut commit, and the post-merge tag + GitHub Release. Use before opening a PR, before merge, when handling a release-cut, and when picking a version bump.
---

# Agnes release process

Source of truth for the rules in `CLAUDE.md § Release process` and
`docs/RELEASING.md`. This skill is invoked by the main agent during planning,
by `agnes-reviewer-rules` during review, and by `agnes-releaser` during the
release-cut. When the rules below conflict with the master documents above,
the master documents win — update this skill.

## When this skill applies

- Opening a PR
- Reviewing a PR (release-cut implications)
- Cutting a release (version bump, CHANGELOG rename)
- Post-merge tagging + GitHub Release

## CHANGELOG discipline

Every PR that changes **user-visible behavior** MUST add a bullet under
`## [Unreleased]` in `CHANGELOG.md`, grouped under Added / Changed / Fixed /
Removed / Internal. Breaking changes are prefixed `**BREAKING**`.

Doc-only PRs (`docs/**`, README) typically do not need a bullet. Apply
judgment based on the diff — if the docs change describes new behavior that
should have shipped with a code change, the *code* PR carries the bullet.

The CHANGELOG entry is part of the PR that introduces the change — never a
follow-up PR.

## Release-cut belongs in the PR

If a PR lands the only `[Unreleased]` content since the last release, the
release-cut is the **last commit on that PR**:

1. Bump `pyproject.toml` (`version = "X.Y.Z"`).
2. Rename `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD`.
3. Add a new empty `## [Unreleased]` above it.

The release-cut is never a standalone follow-up PR.

## Version bump decision

- **Patch** (X.Y.Z+1): default for bug fixes, internal refactors, doc tweaks,
  small features that do not change documented behavior.
- **Minor** (X.Y+1.0): new user-visible features, new APIs, schema migrations
  that are backwards-compatible. **Ask the user before picking minor.**
- **Major** (X+1.0.0): breaking changes, removed APIs, incompatible schema
  changes. Requires explicit user confirmation.

## Post-merge sequence

After the PR with the release-cut is merged to `main`:

1. `git tag vX.Y.Z <merge-sha>`
2. `git push origin vX.Y.Z`
3. `gh release create vX.Y.Z --title "vX.Y.Z" --notes "<CHANGELOG body for [X.Y.Z]>"`

Never tag or release before merge.

## Tests before push

Run `.venv/bin/pytest tests/ --tb=short -n auto -q` before every push.
Failures in code you touched: fix before pushing. Failures unrelated:
confirm they reproduce on a clean branch, note in the PR body, do not block.
```

- [ ] **Step 2: YAML lint**

Run the lint primitive (top of plan) with:
- `<file>` = `.claude/skills/agnes-release-process.md`
- `<required-keys>` = `name,description`

Expected: `OK ...agnes-release-process.md`.

- [ ] **Step 3: Smoke check**

Read the file. Verify all six section headings are present (`When this skill applies`, `CHANGELOG discipline`, `Release-cut belongs in the PR`, `Version bump decision`, `Post-merge sequence`, `Tests before push`). No TODO/TBD/XXX in the body. Cross-reference `CLAUDE.md § Release process` exists (grep `## Release process` in `CLAUDE.md`).

- [ ] **Step 4: Commit**

```bash
git add .claude/skills/agnes-release-process.md
git commit -m "feat(skills): agnes-release-process — CHANGELOG, release-cut, version bump rules"
```

---

## Task 2: Skill `agnes-orchestrator`

**Files:**
- Create: `.claude/skills/agnes-orchestrator.md`

- [ ] **Step 1: Write the file**

Path: `.claude/skills/agnes-orchestrator.md`

```markdown
---
name: agnes-orchestrator
description: Rules for the SyncOrchestrator, the extract.duckdb ATTACH flow, query_mode semantics (local / remote / materialized), and when to call rebuild() vs rebuild_source(). Use when editing src/orchestrator.py, src/db.py, or anything that produces extract.duckdb in connectors/.
---

# Agnes orchestrator

Source of truth for orchestrator invariants. See `CLAUDE.md § Architecture`
and `docs/architecture.md` for the canonical description.

## ATTACH flow

`SyncOrchestrator.rebuild()` scans `/data/extracts/*/extract.duckdb`,
ATTACHes each into the master `analytics.duckdb`, creates views like
`<source>."<bucket>"."<table>"`, and updates `sync_state`.

Per-source rebuild is `rebuild_source(name)` — used after Jira webhooks where
only one source changed. Full `rebuild()` is the fallback when scope is
unclear.

## Thread safety

All write paths take `self._rebuild_lock` (a `threading.Lock`). New write
paths — anything that DETACHes / re-ATTACHes / updates `sync_state` — MUST
hold the lock. Read paths must not hold it.

## query_mode

Every table has a `query_mode` in its `_meta` row:

- `local` — batch-pulled to parquet, queried locally. Parquets live under
  `/data/extracts/<source>/data/`. Synced via `agnes pull`.
- `remote` — queried against the upstream (e.g., BigQuery) at query time.
  No parquet on disk. Requires a `_remote_attach` row in `extract.duckdb`.
- `materialized` — admin-registered SQL run by the scheduler. Result lands as
  a parquet under `/data/extracts/<source>/data/`. Distributed like `local`.

## `_remote_attach` mechanism

For `query_mode='remote'` tables, the extractor writes a `_remote_attach`
table in `extract.duckdb` with columns:

| column | meaning |
|---|---|
| `alias` | name used in the ATTACH statement |
| `extension` | DuckDB extension to install + load |
| `url` | upstream connection URL |
| `token_env` | env var holding the auth token (`''` if extension-specific auth, e.g., BigQuery's GCE metadata server) |

At query time the orchestrator installs/loads the extension, resolves the
token, creates a session-scoped SECRET when required, and ATTACHes the
source so views like `kbc."bucket"."table"` resolve.

## Master DB locations

- System DB: `${DATA_DIR}/state/system.duckdb` (sync_state, table_registry, users, RBAC).
- Analytics DB: `${DATA_DIR}/analytics/server.duckdb` (master views).

## Schema migrations

`src/db.py` auto-migrates from `v1 → vN` on startup. Per-version notes live
in `CHANGELOG.md`. Adding a schema version means:

1. Bumping the version constant in `src/db.py`.
2. Adding the `vN-1 → vN` migration step.
3. Adding a CHANGELOG bullet that names the version.
4. Updating documentation that references the schema version (search for
   "schema v" in `docs/` + `CLAUDE.md`).

## Files NOT to modify

`connectors/jira/file_lock.py`, `connectors/jira/transform.py`,
`services/ws_gateway/` — stable infrastructure.
```

- [ ] **Step 2: YAML lint**

Lint with `name,description`.

- [ ] **Step 3: Smoke check**

Read the file. Verify section headings: `ATTACH flow`, `Thread safety`, `query_mode`, `_remote_attach mechanism`, `Master DB locations`, `Schema migrations`, `Files NOT to modify`. Verify `_rebuild_lock` is mentioned. Grep `CLAUDE.md` for `## Architecture` to confirm the cross-reference resolves.

- [ ] **Step 4: Commit**

```bash
git add .claude/skills/agnes-orchestrator.md
git commit -m "feat(skills): agnes-orchestrator — ATTACH flow, query_mode, _remote_attach"
```

---

## Task 3: Skill `agnes-rbac`

**Files:**
- Create: `.claude/skills/agnes-rbac.md`

- [ ] **Step 1: Write the file**

Path: `.claude/skills/agnes-rbac.md`

```markdown
---
name: agnes-rbac
description: Rules for endpoint gating (require_admin vs require_resource_access), ResourceType registration, and the user_groups model. Use when adding or changing endpoints in app/api/, touching app/auth/, or introducing a new resource type.
---

# Agnes access control

Two-layer model with no role hierarchy. See `CLAUDE.md § Access control` and
`docs/RBAC.md`.

## Tables

- `user_groups` — named groups. `Admin` (god-mode short-circuit on every
  authorization check) and `Everyone` (auto-membership) are seeded as
  `is_system=TRUE`.
- `user_group_members` — `(user_id, group_id, source)`. `source` segregates
  writers so Google's nightly sync does not clobber admin-added members.
- `resource_grants` — `(group, resource_type, resource_id)` triples for any
  entity-scoped grant.

## Gate decision

For every new endpoint, pick one:

- `Depends(require_admin)` — app-level mutations (anything that changes shared
  state without a per-entity scope: registering tables, creating users,
  managing groups, server config).
- `Depends(require_resource_access(ResourceType.X, "{path}"))` — entity-scoped
  reads or mutations. The path expression extracts the `resource_id` from the
  request.

Both imports live in `app.auth.access`.

## Adding a new ResourceType

1. Extend the `ResourceType` `StrEnum` in `app/resource_types.py` with the
   new value.
2. Register a `ResourceTypeSpec` for it in the same file, including a
   `list_blocks` projection delegate that returns the rows visible to a
   given caller.
3. **No DB migration needed** — `resource_grants` is generic.
4. Gate the endpoints that consume the new type with
   `require_resource_access(ResourceType.NEW, "{path}")`.

## Admin layer is the source of truth for auto-sync

For `agnes pull`: `query_mode IN ('local', 'materialized')` plus a
`resource_grants` row for one of the analyst's groups → table appears in
their manifest. There is no per-user sync config.

## Auth providers

Auth providers live in `app/auth/`:

- **Google OAuth** — sign-in via Google. Workspace group memberships are
  pulled at sign-in (see `docs/auth-groups.md` for GCP setup checklist + the
  `security` label gotcha).
- **Email magic link** — itsdangerous token.
- **Desktop JWT** — for the CLI / API.

## Admin UI and CLI

- Admin UI: `/admin/access`.
- CLI: `agnes admin group …` and `agnes admin grant …`.
```

- [ ] **Step 2: YAML lint**

Lint with `name,description`.

- [ ] **Step 3: Smoke check**

Read the file. Verify section headings: `Tables`, `Gate decision`, `Adding a new ResourceType`, `Admin layer is the source of truth for auto-sync`, `Auth providers`, `Admin UI and CLI`. Grep `app/auth/access.py` exists. Grep `app/resource_types.py` exists.

- [ ] **Step 4: Commit**

```bash
git add .claude/skills/agnes-rbac.md
git commit -m "feat(skills): agnes-rbac — gate decision, ResourceType registration"
```

---

## Task 4: Skill `agnes-connectors`

**Files:**
- Create: `.claude/skills/agnes-connectors.md`

- [ ] **Step 1: Write the file**

Path: `.claude/skills/agnes-connectors.md`

```markdown
---
name: agnes-connectors
description: Rules for the extract.duckdb contract every data source must produce — the _meta table, the _remote_attach mechanism for remote-mode tables, parquet layout, and the pattern for adding a new connector. Use when adding a new data source or modifying an existing extractor in connectors/.
---

# Agnes connectors — the extract.duckdb contract

Every data source produces the same output:

    /data/extracts/{source_name}/
    ├── extract.duckdb          ← _meta table + views
    └── data/                   ← parquet files (local sources only)

See `CLAUDE.md § Architecture: extract.duckdb Contract` and
`docs/architecture.md`.

## Required `_meta` table

Every `extract.duckdb` MUST contain a `_meta` table with these columns:

| column | type | meaning |
|---|---|---|
| `table_name` | VARCHAR | name used in views |
| `description` | VARCHAR | human-readable description |
| `rows` | BIGINT | row count at extraction time |
| `size_bytes` | BIGINT | parquet size for local mode, 0 for remote |
| `extracted_at` | TIMESTAMP | extraction time |
| `query_mode` | VARCHAR | one of `local`, `remote`, `materialized` |

If `_meta` is missing or malformed, `SyncOrchestrator.rebuild()` skips the
source with an error logged. Tests for new connectors MUST assert `_meta` is
well-formed.

## Four connector shapes

- **Batch pull** (Keboola, `query_mode='local'`) — DuckDB extension downloads
  data to parquet, scheduled. Extractor in
  `connectors/<name>/extractor.py`.
- **Remote attach** (BigQuery, `query_mode='remote'`) — DuckDB BQ extension,
  no download. Queries hit the upstream at query time. Requires `_remote_attach`.
- **Materialized SQL** (`query_mode='materialized'`) — scheduler runs
  admin-registered SQL through DuckDB and writes the result to a parquet under
  `/data/extracts/<source>/data/`. Distributed via the same manifest +
  `agnes pull` flow as `local`. BigQuery cost guardrail:
  `data_source.bigquery.max_bytes_per_materialize` (default 10 GiB; `0` disables).
- **Real-time push** (Jira) — webhooks update parquets incrementally; the
  webhook handler triggers `rebuild_source('jira')`.

## `_remote_attach` table (remote mode only)

For each remote-mode table in `_meta`, the extractor writes a row in
`_remote_attach` with `alias`, `extension`, `url`, `token_env`. See the
`agnes-orchestrator` skill for how the orchestrator consumes it.

## Adding a new connector — checklist

1. Create `connectors/<name>/extractor.py` that emits `extract.duckdb` (+
   `data/*.parquet` if local) into `/data/extracts/<name>/`.
2. Populate `_meta` with one row per table.
3. If any table is `query_mode='remote'`, populate `_remote_attach`.
4. Register the connector type in the catalog (search for existing
   `source_type` values to follow the pattern).
5. Add a fixture-based test that runs the extractor against a fixture
   upstream and asserts `_meta` is complete.
6. CHANGELOG bullet under `Added` per `agnes-release-process`.

## Stable infrastructure — do NOT modify

`connectors/jira/file_lock.py` and `connectors/jira/transform.py`.
```

- [ ] **Step 2: YAML lint**

Lint with `name,description`.

- [ ] **Step 3: Smoke check**

Read the file. Verify section headings: `Required _meta table`, `Four connector shapes`, `_remote_attach table`, `Adding a new connector — checklist`, `Stable infrastructure`. Verify `connectors/jira/file_lock.py` and `connectors/jira/transform.py` exist (the "do not modify" list must reference real files).

- [ ] **Step 4: Commit**

```bash
git add .claude/skills/agnes-connectors.md
git commit -m "feat(skills): agnes-connectors — extract.duckdb contract, _meta, _remote_attach"
```

---

## Task 5: Subagent `agnes-reviewer-rules`

**Files:**
- Create: `.claude/agents/agnes-reviewer-rules.md`

- [ ] **Step 1: Write the file**

Path: `.claude/agents/agnes-reviewer-rules.md`

```markdown
---
name: agnes-reviewer-rules
description: Use at the end of PR work to enforce Agnes conventions — CHANGELOG bullet (smart, not blind), vendor-agnostic content, no AI attribution, issue economy, clean commits. Fast, runs on every PR.
tools: Read, Bash
model: haiku
---

You are a focused PR reviewer for the Agnes OSS repository. Your job is to
read the diff between the current branch and the base branch and report a
short punch list. You do NOT edit code, never run `Edit` or `Write`, and
never call `gh pr merge`. Your output is markdown.

## Inputs

The main agent passes you:
- The PR's branch name (or just `HEAD` and the base branch).
- Optionally, the PR draft body.

## What to check

For each item, classify as Done / Missing / Warning. Skip items that do not
apply to this diff and say so.

### 1. CHANGELOG bullet

- Read `CHANGELOG.md`. Does it have a new bullet under `## [Unreleased]`
  that matches the diff?
- If yes: Done.
- If no AND the diff changes user-visible behavior: Missing.
- If no AND the diff is doc-only (`docs/**`, `README.md`) or purely
  internal (test refactors, comment fixes): Done with a note explaining why
  no bullet is needed.
- Use the `agnes-release-process` skill for the exact CHANGELOG discipline
  rules. Invoke it with `Skill(agnes-release-process)`.

### 2. Vendor-agnostic content

Grep the diff against the **deployment-specific token list maintained in
`CLAUDE.local.md`** (gitignored, never shipped):

    # Pull the operator's token alternation from CLAUDE.local.md.
    tokens=$(grep -i '^vendor_tokens:' CLAUDE.local.md 2>/dev/null | sed 's/^vendor_tokens: *//')
    if [ -n "$tokens" ]; then
        git diff <base>...HEAD \
            | grep -i -E "$tokens" \
            | grep -v -e '^---' -e '^+++' -e '.claude/agents/agnes-reviewer-rules.md'
    fi
    # Plus always-on common-pattern leaks (cloud project IDs, internal
    # hostnames, SA emails) — wrong in OSS regardless of operator list.
    git diff <base>...HEAD \
        | grep -i -E 'prj-[a-z0-9-]+|gcp-[a-z0-9-]+|\.internal[: /]|\.corp[: /]|[a-z0-9._-]+\.iam\.gserviceaccount\.com' \
        | grep -v -e '^---' -e '^+++'

Expected matches: zero (outside `docs/archive/` and `CHANGELOG.md` historical
entries). Report any other match as Warning with the file:line. The OSS repo
must NOT carry the literal token list — that's why the agent sources it from
`CLAUDE.local.md` instead of inlining customer names here.

### 3. AI attribution

Check commit messages and PR body:

    git log --format='%B' <base>..HEAD | grep -i -E 'co-authored-by: claude|generated with claude|claude code'

Expected: zero matches. Any match is Missing — main agent must remove them
before opening the PR.

### 4. Issue economy

Read the PR body and any new `TODO`/`FIXME` comments in the diff. Red flag:
filing a follow-up issue for something that is either fixable in this PR
(≤30 min, ≤1 file) or moot on current `main`. If found, report as Warning
with a recommendation: fix now, close as moot, or leave a `TODO` in the
touching diff.

### 5. Commit hygiene

`git log --oneline <base>..HEAD`. Red flags:
- Commit message includes AI attribution (already covered above).
- WIP / fixup / squash markers left in messages.
- Commits that should have been amended (e.g., "typo", "lint fix" of an
  immediately preceding commit).

Report each as Warning, naming the SHA.

### 6. Release-cut implication

Invoke `Skill(agnes-release-process)`. Then: would this PR land the only
`[Unreleased]` content since the last tag? If yes, the release-cut commit
must be the last commit on this PR (version bump + CHANGELOG rename + new
empty `[Unreleased]`). Report as Done if present, Missing if not.

## Output format

Markdown, one section per check, three-line max per finding:

    ## CHANGELOG bullet — Done
    Bullet under `## [Unreleased] > Added`: "Add foo to bar."

    ## Vendor-agnostic content — Warning
    `docs/operator/runbook.md:42` mentions `<cloud-project-id>` (matched the `prj-…` pattern). Replace with `<project>` placeholder or move to the operator's private infra repo.

End with a one-line verdict: `OVERALL: ready / needs fixes`.

## Do not

- Do not edit files.
- Do not run tests.
- Do not call `gh pr merge` or push branches.
- Do not invent rules that are not in `CLAUDE.md` or the `agnes-release-process` skill.
```

- [ ] **Step 2: YAML lint**

Lint with `name,description,tools,model`.

- [ ] **Step 3: Smoke check**

Read the file. Verify the six numbered check sections present. Verify "Do not" section includes "Do not edit files."

- [ ] **Step 4: Smoke invocation**

Manually invoke this subagent via the Agent tool with prompt:

> Review the diff `HEAD~1..HEAD` on this branch. Treat it as a PR against `main`. Return your punch list.

(`HEAD~1..HEAD` will be the previous skill commit, so the reviewer should report mostly "doesn't apply" for everything. The point is to confirm it runs and produces a markdown punch list, not to find issues.)

- [ ] **Step 5: Commit**

```bash
git add .claude/agents/agnes-reviewer-rules.md
git commit -m "feat(agents): agnes-reviewer-rules — CHANGELOG, vendor-agnostic, attribution"
```

---

## Task 6: Subagent `agnes-reviewer-rbac`

**Files:**
- Create: `.claude/agents/agnes-reviewer-rbac.md`

- [ ] **Step 1: Write the file**

Path: `.claude/agents/agnes-reviewer-rbac.md`

```markdown
---
name: agnes-reviewer-rbac
description: Use when a PR diff touches app/api/, app/auth/, or app/resource_types.py. Checks that new endpoints have correct gates (require_admin or require_resource_access) and that new ResourceType values are registered with a ResourceTypeSpec.
tools: Read, Grep, Bash
model: sonnet
---

You are a focused security reviewer for Agnes RBAC. Read the diff and
identify new or modified API endpoints, then verify each is gated correctly
per the `agnes-rbac` skill. You do NOT edit code.

## Inputs

The main agent passes you the PR branch (or `HEAD`) and the base branch.
You determine yourself whether the diff is in scope.

## Scope check

In scope iff `git diff --name-only <base>...HEAD` returns at least one path
matching `app/api/**` OR `app/auth/**` OR `app/resource_types.py`. If out
of scope: return a single line "OUT_OF_SCOPE" and stop.

## What to check

### 1. New endpoints have a gate

For each new or modified handler in `app/api/`:

- Locate the handler with `Grep` (e.g., `@router\.(get|post|put|delete|patch)`).
- For each, inspect the function signature for `Depends(require_admin)` or
  `Depends(require_resource_access(ResourceType.X, "{path}"))` — both
  imported from `app.auth.access`.
- If neither: report `MISSING_GATE` with file:line and the route path.
- If present but ambiguous (e.g., a read endpoint with `require_admin` when
  a resource-scoped gate would be more appropriate): report `AMBIGUOUS` with
  rationale.

Invoke `Skill(agnes-rbac)` for the gate decision rules.

### 2. New ResourceType values are registered

`git diff <base>...HEAD app/resource_types.py`. If the diff adds an enum
member to `ResourceType`:

- Verify the same diff adds a `ResourceTypeSpec` registration for that
  enum value.
- Verify the spec includes a `list_blocks` projection delegate.

If anything missing: report `INCOMPLETE_RESOURCE_TYPE`.

### 3. `Admin` group short-circuit not bypassed

Greps for any new `require_admin` reimplementation outside `app.auth.access`.
Should be zero.

## Output format

Markdown, one section per finding:

    ## MISSING_GATE
    `app/api/foo.py:42` — `POST /foo/bar` has no `Depends(require_admin)` or `Depends(require_resource_access(...))`.

    ## OK
    `app/api/baz.py:88` — `GET /baz/{id}` correctly gated with `Depends(require_resource_access(ResourceType.BAZ, "{id}"))`.

End with verdict: `OVERALL: all endpoints gated / N missing / N ambiguous`.

## Do not

- Do not edit files.
- Do not invent gates — if rules are unclear, report `AMBIGUOUS` and let the main agent decide.
```

- [ ] **Step 2: YAML lint**

Lint with `name,description,tools,model`.

- [ ] **Step 3: Smoke check**

Read the file. Verify section headings. Verify `app.auth.access` is mentioned (real Python module). Verify `app/resource_types.py` is referenced (run `ls app/resource_types.py` — must exist).

- [ ] **Step 4: Smoke invocation**

Invoke via Agent tool with prompt:

> Review `HEAD~5..HEAD` on this branch against `main`. Apply the RBAC reviewer.

Expected: `OUT_OF_SCOPE` (no app/api or app/auth changes in this branch yet).

- [ ] **Step 5: Commit**

```bash
git add .claude/agents/agnes-reviewer-rbac.md
git commit -m "feat(agents): agnes-reviewer-rbac — endpoint gates, ResourceType registration"
```

---

## Task 7: Subagent `agnes-reviewer-architecture`

**Files:**
- Create: `.claude/agents/agnes-reviewer-architecture.md`

- [ ] **Step 1: Write the file**

Path: `.claude/agents/agnes-reviewer-architecture.md`

```markdown
---
name: agnes-reviewer-architecture
description: Use when a PR diff touches src/orchestrator.py, src/db.py, connectors/*/extractor.py, or adds a schema migration. Checks extract.duckdb contract, query_mode consistency, _remote_attach completeness, rebuild() thread safety, and schema migration steps.
tools: Read, Grep, Bash
model: sonnet
---

You are a focused architecture reviewer for Agnes core. Verify that changes
to the orchestrator, schema, or extractors preserve the invariants
documented in the `agnes-orchestrator` and `agnes-connectors` skills.

## Scope check

In scope iff `git diff --name-only <base>...HEAD` returns at least one path
matching:
- `src/orchestrator.py`
- `src/db.py`
- `connectors/*/extractor.py`
- `connectors/*/extract_init.py`
- Any new file under `connectors/`

If out of scope: return `OUT_OF_SCOPE` and stop.

## What to check

Invoke `Skill(agnes-orchestrator)` and `Skill(agnes-connectors)` to load the
rules.

### 1. `_meta` table contract (extractor changes)

For each modified extractor, verify the produced `_meta` table has all six
required columns: `table_name`, `description`, `rows`, `size_bytes`,
`extracted_at`, `query_mode`. Search the extractor source for the table
creation / insert statements.

If any column is missing: `BROKEN: _meta_missing_column`.

### 2. `_remote_attach` completeness (remote-mode changes)

If the diff adds or modifies a `query_mode='remote'` table, verify
`_remote_attach` is populated with `alias`, `extension`, `url`, `token_env`.

If missing: `BROKEN: remote_attach_incomplete`.

### 3. Schema migration (`src/db.py` changes)

If `src/db.py` bumps the version constant, verify:
- A migration step `vN-1 → vN` exists in the same diff.
- `CHANGELOG.md` has a bullet under `Internal` naming the new version.
- Any doc that references "schema v" mentions the new version.

If any missing: `BROKEN: schema_migration_incomplete`.

### 4. `rebuild()` thread safety

If the diff modifies `rebuild()` or `rebuild_source()`, verify all write
paths take `self._rebuild_lock`. Search the diff for any new DETACH /
re-ATTACH / sync_state mutation outside the lock.

If found: `BROKEN: lock_not_held`.

### 5. `query_mode` consistency

For new tables added to `_meta`, `query_mode` must be one of `local`,
`remote`, `materialized`. Anything else: `BROKEN: invalid_query_mode`.

## Output format

Markdown, one section per finding:

    ## HOLDS
    `_meta` table contract — extractor populates all six required columns.

    ## BROKEN: schema_migration_incomplete
    `src/db.py` bumps to v40 but no `_migrate_v39_to_v40` defined.

End with verdict: `OVERALL: all invariants hold / N broken / N unclear`.

## Do not

- Do not edit files.
- Do not run extractors (no network calls).
- Do not infer invariants not in the cited skills.
```

- [ ] **Step 2: YAML lint**

Lint with `name,description,tools,model`.

- [ ] **Step 3: Smoke check**

Read the file. Verify section headings. Verify the four "Files NOT to modify"–level files referenced are real: `src/orchestrator.py`, `src/db.py` exist.

- [ ] **Step 4: Smoke invocation**

Invoke via Agent tool:

> Review `HEAD~5..HEAD` against `main`. Apply the architecture reviewer.

Expected: `OUT_OF_SCOPE`.

- [ ] **Step 5: Commit**

```bash
git add .claude/agents/agnes-reviewer-architecture.md
git commit -m "feat(agents): agnes-reviewer-architecture — extract.duckdb, schema, rebuild lock"
```

---

## Task 8: Subagent `agnes-releaser`

**Files:**
- Create: `.claude/agents/agnes-releaser.md`

- [ ] **Step 1: Write the file**

Path: `.claude/agents/agnes-releaser.md`

```markdown
---
name: agnes-releaser
description: Use before merging a PR (phase 1 — prepare release-cut commit) and after merge (phase 2 — tag + GitHub Release). Invoked explicitly by the user; never auto-fires. Never merges the PR.
tools: Read, Edit, Bash
model: sonnet
---

You handle the Agnes release-cut workflow. There are two phases. The main
agent or user names which phase when invoking you.

Invoke `Skill(agnes-release-process)` first — it carries the current rules
and the version-bump decision tree.

## Phase 1 — pre-merge

Triggered by the user / main agent saying "ready to merge" or similar.

1. **Determine scope.** Run `git log --oneline $(git describe --tags --abbrev=0)..HEAD` to see commits since the last tag. If this branch is the source of all `[Unreleased]` content, phase 1 applies. If `[Unreleased]` is already empty or has content from other merged PRs only, phase 1 does NOT apply — return `NO_RELEASE_CUT_NEEDED` and stop.

2. **Pick version.** Read `pyproject.toml` for the current version. Per the rules in `Skill(agnes-release-process)`:
   - Default to patch (`X.Y.Z+1`).
   - If the diff adds user-visible features or schema migrations: ask the user "minor bump (X.Y+1.0)?" — wait for confirmation.
   - If the diff has `**BREAKING**` entries: ask the user "major bump (X+1.0.0)?" — wait for confirmation.

3. **Prepare the release-cut commit:**
   - Update `pyproject.toml` `version = "X.Y.Z"`.
   - In `CHANGELOG.md`: rename `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD` (today's date). Insert a new empty `## [Unreleased]` section above it with empty subsection headers (`### Added`, `### Changed`, `### Fixed`, `### Removed`, `### Internal`).

4. **Stage and commit:**
   ```bash
   git add pyproject.toml CHANGELOG.md
   git commit -m "release: X.Y.Z — <one-line summary from CHANGELOG>"
   git push
   ```

5. **Report:** print the version, the commit SHA, and a one-line summary. Tell the user: "release-cut commit pushed. Merge the PR yourself when ready."

You do NOT run `gh pr merge`.

## Phase 2 — post-merge

Triggered by the user / main agent saying "tag it" or similar after merge.

1. **Confirm merge.** Run `git fetch origin main` then `git log --oneline -5 origin/main`. Identify the merge commit. Verify it includes the release-cut diff (the version bump in `pyproject.toml` and the `[X.Y.Z]` heading in `CHANGELOG.md`).

2. **Tag:**
   ```bash
   git tag -a vX.Y.Z <merge-sha> -m "vX.Y.Z"
   git push origin vX.Y.Z
   ```

3. **GitHub Release.** Extract the body of the `[X.Y.Z]` section from `CHANGELOG.md` (everything between the `## [X.Y.Z]` heading and the next `##` heading).

   ```bash
   gh release create vX.Y.Z --title "vX.Y.Z" --notes "$(cat <<'EOF'
   <extracted CHANGELOG body>
   EOF
   )"
   ```

4. **Report:** print the GitHub Release URL.

## Never do

- Never run `gh pr merge`.
- Never `git push --force`.
- Never amend commits that are already on `main`.
- Never tag before merge.
- Never proceed without user confirmation on minor or major bumps.

If something is unclear (e.g., last tag missing, CHANGELOG malformed),
report the issue and stop — do not improvise.
```

- [ ] **Step 2: YAML lint**

Lint with `name,description,tools,model`.

- [ ] **Step 3: Smoke check**

Read the file. Verify both phases present, each with numbered steps. Verify "Never do" section includes "Never run `gh pr merge`."

- [ ] **Step 4: Smoke invocation (dry-run, phase 1)**

Do NOT actually run the releaser against the current branch — it would attempt a real commit. Instead, invoke with a dry-run prompt:

> You are agnes-releaser. Phase 1, but DO NOT make any commits or push. Just walk me through what you would do for the current branch as if it were merging to main. Report the version, the diff you would prepare, and the commit message.

Expected: textual walkthrough, no actual `git commit`.

- [ ] **Step 5: Commit**

```bash
git add .claude/agents/agnes-releaser.md
git commit -m "feat(agents): agnes-releaser — pre-merge release-cut + post-merge tag/release"
```

---

## Task 9: CLAUDE.md pointer

**Files:**
- Modify: `CLAUDE.md` — add a short section near the existing `## Project conventions` block.

- [ ] **Step 1: Find the insertion point**

Run: `grep -n "^## " CLAUDE.md | tail -10`
Identify the line number of `## Project conventions`.

- [ ] **Step 2: Insert a new section above it**

Add the following section immediately before `## Project conventions`:

```markdown
## Specialized agents and skills

Two committed locations carry Agnes-specific Claude Code behavior:

- `.claude/skills/agnes-*.md` — knowledge skills (`agnes-orchestrator`, `agnes-rbac`, `agnes-connectors`, `agnes-release-process`). Loaded into the main agent's context when their description matches the work, or invoked explicitly via `Skill(<name>)`. Read these before editing the corresponding part of the codebase.
- `.claude/agents/agnes-*.md` — specialist subagents (`agnes-reviewer-rules`, `agnes-reviewer-rbac`, `agnes-reviewer-architecture`, `agnes-releaser`). Spawned via the Agent tool at the end of PR work (reviewers, in parallel) or explicitly before/after merge (releaser).

Design rationale: `docs/superpowers/specs/2026-05-15-agnes-agents-design.md`.

```

- [ ] **Step 3: Verify the insertion**

Run: `grep -n "^## " CLAUDE.md | head -20`
Verify `## Specialized agents and skills` appears once and before `## Project conventions`.

- [ ] **Step 4: Add a CHANGELOG bullet**

In `CHANGELOG.md` under `## [Unreleased]`:

```markdown
### Added
- Specialized Claude Code agents and skills for Agnes development. Knowledge skills under `.claude/skills/` (orchestrator, RBAC, connectors, release-process); reviewer + releaser subagents under `.claude/agents/`. Design: `docs/superpowers/specs/2026-05-15-agnes-agents-design.md`.
```

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md CHANGELOG.md
git commit -m "docs(claude-md): pointer to .claude/skills and .claude/agents"
```

---

## Task 10: End-to-end smoke test

Goal: simulate a typical PR flow and confirm the main agent invokes the
right reviewers in parallel. This is a manual sanity check, not a code test.

- [ ] **Step 1: Set up a trivial fake change**

In the worktree:

```bash
git checkout -b zs/smoke-test-agnes-agents
echo "# placeholder" >> docs/architecture.md
git add docs/architecture.md
git commit -m "docs: add placeholder line for smoke test"
```

- [ ] **Step 2: Invoke `agnes-reviewer-rules`**

From the main agent: `Agent(agnes-reviewer-rules)` with prompt:

> Review the branch `zs/smoke-test-agnes-agents` against `main`. Treat it as a PR.

Expected output: punch list. Specifically:
- CHANGELOG bullet: Done with note (doc-only PR).
- Vendor-agnostic: Done.
- AI attribution: Done.
- Issue economy: Done (no follow-up issues).
- Commit hygiene: Done.
- Release-cut implication: probably Done with note (this is the only `[Unreleased]` content, so release-cut would be needed if this were a real release-bound PR, but a smoke test doesn't trigger one).
- `OVERALL: ready` or `needs fixes` (depending on exact rules).

- [ ] **Step 3: Verify scope checks fire**

Invoke `Agent(agnes-reviewer-rbac)` with the same prompt. Expected: `OUT_OF_SCOPE` (no app/api or app/auth changes).

Invoke `Agent(agnes-reviewer-architecture)` with the same prompt. Expected: `OUT_OF_SCOPE` (no src/ or connectors/ changes).

- [ ] **Step 4: Clean up**

```bash
git checkout worktree-zs+agnes-agents-spec
git branch -D zs/smoke-test-agnes-agents
```

- [ ] **Step 5: Final review of all committed files**

```bash
ls .claude/skills .claude/agents
git log --oneline origin/main..HEAD
```

Expected: 8 files in `.claude/skills/` + `.claude/agents/` combined, ~10 commits since `origin/main`.

- [ ] **Step 6: Open a PR (when satisfied)**

Not part of this plan — defer to the user.

---

## Self-review notes

- **Spec coverage:** every component in the spec (4 skills, 3 reviewers, 1 releaser, CLAUDE.md pointer) has a corresponding task. Personal layer is explicitly out-of-scope.
- **Cross-references:** every "see CLAUDE.md § X" cited in a skill body matches a real heading in the current `CLAUDE.md`. Every file path referenced (`app/auth/access.py`, `app/resource_types.py`, `src/orchestrator.py`, `src/db.py`, `connectors/jira/file_lock.py`) exists in the repo.
- **No placeholders:** every step contains exact commands or file content.
- **Order:** skills land before the agents that consume them (release-process before reviewer-rules + releaser; orchestrator + connectors before reviewer-architecture; rbac before reviewer-rbac). Setup task is first.
