# Agnes Dev-Agent Kit — Slice A (Review Core) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the review core — a `CONTRIBUTING.md` sync-map, a parity reviewer, a consolidator, and the `/agnes-review` Team command — guarded by two structural integrity tests.

**Architecture:** Most artifacts are Claude Code prose (markdown agents/command + a docs file), which are not behavior-testable. Two pytest files give a real red→green loop: (1) `tests/test_contributing_sync_map.py` asserts every load-bearing path the sync-map references still exists on disk **and** appears in the doc; (2) `tests/test_dev_agent_kit.py` asserts the new agents/command have valid frontmatter and that `/agnes-review` only references agents that exist. The reviewer agents themselves are scope-gated subagents spawned by the command as a Team.

**Tech Stack:** Python 3 + pytest (dependency-free file parsing — no PyYAML needed), Claude Code agent/command/`settings` markdown, `git`, `gh`.

Spec: `docs/superpowers/specs/2026-06-05-agnes-dev-agent-kit-design.md` (§4–6, §11, §12 slice A).

---

## File Structure

- Create: `tests/test_contributing_sync_map.py` — sync-map ↔ filesystem ↔ doc integrity guard.
- Create: `tests/test_dev_agent_kit.py` — agent/command frontmatter + cross-reference guard.
- Create: `CONTRIBUTING.md` — sync-map table + review playbook + dev workflow.
- Create: `.claude/agents/agnes-reviewer-parity.md` — DuckDB↔PG parity reviewer.
- Create: `.claude/agents/agnes-review-consolidator.md` — merges findings into one report.
- Create: `.claude/commands/agnes-review.md` — scope-gated review Team command.
- Modify: `.claude/agents/agnes-reviewer-rules.md`, `agnes-reviewer-architecture.md`, `agnes-reviewer-rbac.md` — point each at the `CONTRIBUTING.md` sync-map.

Helper conventions used by both test files (defined inline in each, DRY within a file):
- `REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent`
- `read_frontmatter(path) -> dict[str, str]` — minimal parser: returns single-line `key: value` pairs found in the leading `---`-delimited block (enough to check `name`/`description`/`tools`/`allowed-tools` presence; multi-line YAML values are treated as "present").

---

## Task 1: Sync-map integrity guard + `CONTRIBUTING.md`

**Files:**
- Create: `tests/test_contributing_sync_map.py`
- Create: `CONTRIBUTING.md`

- [ ] **Step 1: Write the failing test**

Create `tests/test_contributing_sync_map.py`:

```python
"""Guard: the CONTRIBUTING.md sync-map must reference only real, current paths.

The sync-map (docs/superpowers/specs/2026-06-05-agnes-dev-agent-kit-design.md §5)
is the single source of truth for the review team. If a referenced file is
renamed or deleted, this test fails so the doc is updated in the same change.
"""
from __future__ import annotations

import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
CONTRIBUTING = REPO_ROOT / "CONTRIBUTING.md"

# Load-bearing paths the sync-map references. Each MUST exist on disk AND appear
# (in backticks) in CONTRIBUTING.md. Verified real at 0.66.1; keep in sync.
REFERENCED_PATHS = [
    "src/repositories/__init__.py",
    "src/db.py",
    "app/resource_types.py",
    "app/auth/access.py",
    "CHANGELOG.md",
    "connectors/keboola/extractor.py",
    "tests/db_pg/test_backend_split_guard.py",
    "tests/db_pg/_parity_sweep_util.py",
    "tests/test_db_schema_version.py",
    "tests/test_design_system_contract.py",
]


def test_contributing_exists_with_sync_map_heading():
    assert CONTRIBUTING.exists(), "CONTRIBUTING.md must exist at repo root"
    text = CONTRIBUTING.read_text(encoding="utf-8")
    assert "## Sync-map" in text, "CONTRIBUTING.md must have a '## Sync-map' section"


def test_referenced_paths_exist_on_disk():
    missing = [p for p in REFERENCED_PATHS if not (REPO_ROOT / p).exists()]
    assert not missing, f"sync-map references nonexistent paths: {missing}"


def test_referenced_paths_appear_in_doc():
    text = CONTRIBUTING.read_text(encoding="utf-8")
    absent = [p for p in REFERENCED_PATHS if f"`{p}`" not in text]
    assert not absent, f"paths in REFERENCED_PATHS but not in CONTRIBUTING.md: {absent}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_contributing_sync_map.py -v`
Expected: FAIL — `test_contributing_exists_with_sync_map_heading` asserts (no `CONTRIBUTING.md`).

- [ ] **Step 3: Create `CONTRIBUTING.md`**

Create `CONTRIBUTING.md` at the repo root:

````markdown
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

## Sync-map

Surfaces that must change together — and that CI does **not** fully guard. When
you touch the left column, update the middle column **in the same change**. Each
review finding cites two `file:line`: where the change landed, and where the
mirror is missing.

| Change | Mirror surface that MUST update | Severity | CI guard? |
|---|---|---|---|
| Method in `src/repositories/X.py` | sibling in `src/repositories/X_pg.py` | BLOCKING | partial |
| New repo class (either backend) | dispatch entry in `src/repositories/__init__.py` factory table (symmetric across backends) | BLOCKING | `tests/db_pg/test_backend_split_guard.py` (static) |
| New callsite reading app-state | go through a `*_repo()` factory fn — never direct repo instantiation or raw `get_system_db()` | BLOCKING | `tests/db_pg/test_backend_split_guard.py` (static) + `tests/db_pg/_parity_sweep_util.py` (dynamic) |
| New repo method | extend the matching `tests/db_pg/test_<cluster>_contract.py` | BLOCKING | partial |
| Alembic migration (PG) | matching `_vN_to_v(N+1)` in `src/db.py`; both ladders reach the same `SCHEMA_VERSION` | BLOCKING | `tests/test_db_schema_version.py` |
| New `ResourceType` enum value | `ResourceTypeSpec` in `app/resource_types.py` `RESOURCE_TYPES` | BLOCKING | NO |
| New entity-scoped endpoint | `Depends(require_admin)` or `require_resource_access(...)` from `app/auth/access.py` | BLOCKING | NO |
| User-visible behavior change | `## [Unreleased]` bullet in `CHANGELOG.md` | BLOCKING | NO |
| New connector extractor | `_meta` table contract (`table_name, description, rows, size_bytes, extracted_at, query_mode`) | BLOCKING | partial |
| `query_mode='remote'` table | `_remote_attach` row in `extract.duckdb` | BLOCKING | NO |
| New web page | extends `base_ds.html` / `base_page.html` (never `base.html`); CSS in `head_extra` | BLOCKING | `tests/test_design_system_contract.py` (partial) |
| PR landing the only `[Unreleased]` content | release-cut commit (version bump + CHANGELOG rename) in the same merge | per release rules | NO |

### Parity enforcement reality

Parity is not just `X.py` ↔ `X_pg.py`. Backend selection lives in
`src/repositories/__init__.py` (a `{backend: (module, class)}` dispatch table
keyed off `use_pg()` / `DATABASE_URL`); callsites import `*_repo()` factory
functions, not repo classes. Two guards back the sync-map:

- **Static:** `tests/db_pg/test_backend_split_guard.py` scans for direct repo
  instantiation + `get_system_db()` callers.
- **Dynamic:** `tests/db_pg/_parity_sweep_util.py` drives both backends through a
  `TestClient` and diffs the HTTP status of every parameter-free route.

The parity reviewer flags exactly what these guards cannot see.
````

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_contributing_sync_map.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add tests/test_contributing_sync_map.py CONTRIBUTING.md
git commit -m "feat(dev-kit): add CONTRIBUTING.md sync-map + integrity guard"
```

---

## Task 2: Dev-agent-kit structural guard + parity reviewer

**Files:**
- Create: `tests/test_dev_agent_kit.py`
- Create: `.claude/agents/agnes-reviewer-parity.md`

- [ ] **Step 1: Write the failing test**

Create `tests/test_dev_agent_kit.py`:

```python
"""Guard: dev-agent-kit agents/commands are well-formed and cross-consistent.

Agents/commands are prose, but their frontmatter and cross-references are
structural — a renamed agent or a command pointing at a missing agent is a real
bug this catches.
"""
from __future__ import annotations

import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
AGENTS_DIR = REPO_ROOT / ".claude" / "agents"
COMMANDS_DIR = REPO_ROOT / ".claude" / "commands"


def read_frontmatter(path: pathlib.Path) -> dict[str, str]:
    """Return single-line `key: value` pairs from the leading --- block.

    Multi-line YAML values (e.g. `description: >`) record the key with an empty
    value — presence is what we assert, not the full value.
    """
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    block = text[3:end]
    out: dict[str, str] = {}
    for line in block.splitlines():
        if line[:1].isspace() or ":" not in line:
            continue  # nested/continuation line
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip()
    return out


def agent_names() -> set[str]:
    return {p.stem for p in AGENTS_DIR.glob("*.md")}


def test_parity_reviewer_has_valid_frontmatter():
    path = AGENTS_DIR / "agnes-reviewer-parity.md"
    assert path.exists(), "agnes-reviewer-parity.md must exist"
    fm = read_frontmatter(path)
    assert fm.get("name") == "agnes-reviewer-parity", "name must match filename"
    assert "description" in fm, "agent must declare a description"
    assert "tools" in fm, "agent must declare its tools"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_dev_agent_kit.py -v`
Expected: FAIL — `agnes-reviewer-parity.md must exist`.

- [ ] **Step 3: Create `.claude/agents/agnes-reviewer-parity.md`**

```markdown
---
name: agnes-reviewer-parity
description: Use when a PR diff touches src/repositories/*, src/db.py, migrations/, or tests/db_pg/. Verifies DuckDB↔Postgres parity — matching _pg.py sibling, factory dispatch entry, contract test, and the Alembic↔db.py migration ladder — and flags backend-split drift the existing guards cannot see.
tools: Read, Grep, Bash
model: sonnet
---

You are the dual-backend parity reviewer for Agnes. Both DuckDB and Postgres are
first-class state backends; parity gaps accrue commit-by-commit. Read-only: you
never edit, switch branches, push, or post a GitHub review — you return findings
to the consolidator (or the user).

## Scope check

In scope iff `git diff --name-only <base>...HEAD` returns at least one path
matching: `src/repositories/*`, `src/db.py`, `migrations/`, or `tests/db_pg/`.
If out of scope, return `{"in_scope": false, "findings": []}` and stop.

## Playbook (walk the CONTRIBUTING.md sync-map parity rows)

Read `CONTRIBUTING.md` → "Sync-map" + "Parity enforcement reality" first.

1. **`_pg.py` sibling.** For each changed `src/repositories/X.py`, confirm
   `src/repositories/X_pg.py` changed too (and vice versa). A one-sided change is
   BLOCKING. Cite both paths.
2. **Factory dispatch.** New repo class? Confirm it is registered symmetrically in
   the `src/repositories/__init__.py` dispatch table. Missing → BLOCKING.
3. **No raw reads.** New callsites must use a `*_repo()` factory fn, not direct
   instantiation or `get_system_db()`. Note that `tests/db_pg/test_backend_split_guard.py`
   ratchets this statically — if the diff adds a callsite the ratchet would miss
   (e.g. behind a dynamic import), flag it BLOCKING.
4. **Contract test.** New repo method without an extended
   `tests/db_pg/test_<cluster>_contract.py` → BLOCKING.
5. **Migration ladder.** An Alembic revision under `migrations/` must have a matching
   `_vN_to_v(N+1)` in `src/db.py`, and both reach the same `SCHEMA_VERSION`.
   Mismatch → BLOCKING.

## Severity

BLOCKING (parity/ladder/security gap), NON-BLOCKING (should-fix, not a blocker),
NIT (cosmetic). When unsure, default NON-BLOCKING.

## Output

Return JSON only:

    {"in_scope": true,
     "findings": [
       {"severity": "BLOCKING|NON-BLOCKING|NIT",
        "title": "<short>",
        "introduced_at": "<file:line in the diff>",
        "mirror_missing_at": "<file path that should have changed>",
        "detail": "<=80 words"}
     ]}

Every finding cites both `introduced_at` and `mirror_missing_at`. Verify claims
with real `git diff` / `grep` commands — do not assume.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_dev_agent_kit.py -v`
Expected: PASS (1 passed).

- [ ] **Step 5: Commit**

```bash
git add tests/test_dev_agent_kit.py .claude/agents/agnes-reviewer-parity.md
git commit -m "feat(dev-kit): add parity reviewer agent + structural guard"
```

---

## Task 3: Consolidator agent

**Files:**
- Modify: `tests/test_dev_agent_kit.py` (add a test)
- Create: `.claude/agents/agnes-review-consolidator.md`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_dev_agent_kit.py`:

```python
def test_consolidator_has_valid_frontmatter():
    path = AGENTS_DIR / "agnes-review-consolidator.md"
    assert path.exists(), "agnes-review-consolidator.md must exist"
    fm = read_frontmatter(path)
    assert fm.get("name") == "agnes-review-consolidator", "name must match filename"
    assert "description" in fm, "agent must declare a description"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_dev_agent_kit.py::test_consolidator_has_valid_frontmatter -v`
Expected: FAIL — `agnes-review-consolidator.md must exist`.

- [ ] **Step 3: Create `.claude/agents/agnes-review-consolidator.md`**

```markdown
---
name: agnes-review-consolidator
description: Final consolidator for the agnes-review team. Merges per-reviewer findings into one advisory report — dedup, severity escalation on multi-reviewer overlap, ≤15 findings, file:line + severity each.
tools: Read, Bash
model: sonnet
---

You merge the agnes-review team's findings into one advisory report. You are the
last task in the team (blocked-by all reviewers). Read-only.

## Inputs

The parent passes each reviewer's JSON findings (parity, rules, architecture,
rbac — only the in-scope subset ran). Parse them.

## Merge rules

1. **Dedup.** Same `introduced_at` + same root cause across reviewers → one entry,
   crediting all sources.
2. **Escalate.** If two+ reviewers independently flag the same area, bump severity
   one level.
3. **Cap.** ≤15 findings total. If more, keep the highest-signal 15 and note how
   many were dropped.
4. **Default down.** When a finding's severity is ambiguous, prefer NON-BLOCKING.

## Output report (Markdown)

```
# Review — <branch> (base <base>)

## Verdict
- Verdict: APPROVE | REQUEST CHANGES | COMMENT   (REQUEST CHANGES iff ≥1 BLOCKING)
- Blocking: N · Non-blocking: N · Nits: N · Dropped: N

## Blocking findings
### [B-1] `<introduced_at>` — <title>
- Mirror missing: `<mirror_missing_at>`
- Source: <reviewer(s)>
- <detail>

## Non-blocking findings
### [NB-1] ...

## Nits
### [N-1] ...

## Verification log
- <reviewer>: <what it actually checked>
```

Skip an empty section with `(none)`, never omit it. English in any GitHub-posted
body; the short summary back to the parent may be in the parent's language.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_dev_agent_kit.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add tests/test_dev_agent_kit.py .claude/agents/agnes-review-consolidator.md
git commit -m "feat(dev-kit): add review consolidator agent"
```

---

## Task 4: `/agnes-review` command (cross-reference guarded)

**Files:**
- Modify: `tests/test_dev_agent_kit.py` (add a test)
- Create: `.claude/commands/agnes-review.md`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_dev_agent_kit.py`:

```python
import re


def test_agnes_review_command_references_only_existing_agents():
    path = COMMANDS_DIR / "agnes-review.md"
    assert path.exists(), "agnes-review.md command must exist"
    fm = read_frontmatter(path)
    assert "description" in fm, "command must declare a description"
    assert "allowed-tools" in fm, "command must declare allowed-tools"

    text = path.read_text(encoding="utf-8")
    # Every agnes-review* agent token in the command must be a real agent file.
    # Discard the bare command/team name "agnes-review" (not an agent).
    referenced = set(re.findall(r"agnes-review[\w-]*", text))
    referenced.discard("agnes-review")
    known = agent_names()
    unknown = sorted(referenced - known)
    assert not unknown, f"command references unknown agents: {unknown}"


def test_required_reviewers_present_for_command():
    # The command's roster must all exist as agents.
    roster = {
        "agnes-reviewer-rules",
        "agnes-reviewer-architecture",
        "agnes-reviewer-rbac",
        "agnes-reviewer-parity",
        "agnes-review-consolidator",
    }
    missing = sorted(roster - agent_names())
    assert not missing, f"missing roster agents: {missing}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_dev_agent_kit.py -v`
Expected: FAIL — `agnes-review.md command must exist`.

- [ ] **Step 3: Create `.claude/commands/agnes-review.md`**

````markdown
---
description: Scope-gated PR/diff review. Detects which reviewers the diff touches, runs them as a Team in parallel, consolidates into one advisory report (file:line + severity, ≤15 findings). Read-only working tree; optionally posts ONE comment-only review when an open PR exists. Never approves, requests changes, merges, or edits.
allowed-tools: Task, Bash, Read, Grep, TeamCreate, TeamDelete, TaskCreate, TaskUpdate, TaskList, TaskGet, SendMessage
argument-hint: "[optional base ref; defaults to merge-base with origin/main]"
---

# /agnes-review — scope-gated review team

## 1. Resolve scope

```bash
BASE="${ARGUMENTS:-$(git merge-base origin/main HEAD)}"
git diff --name-only "$BASE"...HEAD
```

Map changed paths → in-scope reviewers:

| Reviewer | Fires when a changed path matches |
|---|---|
| `agnes-reviewer-rules` | always |
| `agnes-reviewer-architecture` | `src/orchestrator.py`, `src/db.py`, `connectors/*/extractor.py`, `connectors/*/extract_init.py`, new `connectors/**` |
| `agnes-reviewer-rbac` | `app/api/`, `app/auth/`, `app/resource_types.py` |
| `agnes-reviewer-parity` | `src/repositories/`, `src/db.py`, `migrations/`, `tests/db_pg/` |

`agnes-reviewer-rules` always runs; add the others only if their paths matched.

## 2. Run the team

```
TeamCreate(team_name="agnes-review", description="Scope-gated review")
# one TaskCreate per in-scope reviewer (no deps)
# TaskCreate(consolidator) with addBlockedBy = [<reviewer task ids>]
```

Spawn each in-scope reviewer via the Task tool: `subagent_type` = the reviewer
name, `team_name="agnes-review"`, `run_in_background=true`, prompt = "Review
`$BASE`...HEAD per your playbook. Return JSON findings. Read-only." Pass `$BASE`.

Wait for all reviewer tasks to complete, then spawn `agnes-review-consolidator`
(`subagent_type="agnes-review-consolidator"`) with all reviewer findings.

## 3. Output

Print the consolidator's report to the terminal. Then check for an open PR:

```bash
gh pr view --json number,state 2>/dev/null
```

If an OPEN PR exists, ask the user whether to post; on yes, post ONE comment-only
review:

```bash
gh pr review <N> --comment --body-file <report.md>
```

NEVER `--approve`, `--request-changes`, `gh pr merge`, `git push`, or edit files.

## 4. Clean up

`TeamDelete(team_name="agnes-review")`. The working tree is untouched — fixes are
a separate follow-up step (main agent or `agnes-builder`).
````

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_dev_agent_kit.py -v`
Expected: PASS (4 passed) — assumes the three existing reviewers already exist (they do).

- [ ] **Step 5: Commit**

```bash
git add tests/test_dev_agent_kit.py .claude/commands/agnes-review.md
git commit -m "feat(dev-kit): add /agnes-review scope-gated team command"
```

---

## Task 5: Point existing reviewers at the sync-map

**Files:**
- Modify: `tests/test_dev_agent_kit.py` (add a test)
- Modify: `.claude/agents/agnes-reviewer-rules.md`
- Modify: `.claude/agents/agnes-reviewer-architecture.md`
- Modify: `.claude/agents/agnes-reviewer-rbac.md`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_dev_agent_kit.py`:

```python
import pytest


@pytest.mark.parametrize("agent", [
    "agnes-reviewer-rules",
    "agnes-reviewer-architecture",
    "agnes-reviewer-rbac",
    "agnes-reviewer-parity",
])
def test_reviewers_reference_sync_map(agent):
    text = (AGENTS_DIR / f"{agent}.md").read_text(encoding="utf-8")
    assert "CONTRIBUTING.md" in text, (
        f"{agent} must point at the CONTRIBUTING.md sync-map"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_dev_agent_kit.py -k reference_sync_map -v`
Expected: FAIL for `rules`, `architecture`, `rbac` (parity already references it).

- [ ] **Step 3: Add a sync-map pointer to each existing reviewer**

In each of `agnes-reviewer-rules.md`, `agnes-reviewer-architecture.md`,
`agnes-reviewer-rbac.md`, add this line immediately after the first paragraph of
the agent body (after the frontmatter's closing `---` and the opening prose):

```markdown
Before reviewing, read the sync-map in `CONTRIBUTING.md` — it lists the surfaces
that must change together and that CI does not guard. Walk the rows relevant to
your scope and cite both `file:line` (where the change landed + where the mirror
is missing).
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_dev_agent_kit.py -k reference_sync_map -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add tests/test_dev_agent_kit.py .claude/agents/agnes-reviewer-rules.md .claude/agents/agnes-reviewer-architecture.md .claude/agents/agnes-reviewer-rbac.md
git commit -m "feat(dev-kit): point existing reviewers at CONTRIBUTING.md sync-map"
```

---

## Task 6: Full-suite check + CHANGELOG

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Run the two new test files together**

Run: `.venv/bin/pytest tests/test_contributing_sync_map.py tests/test_dev_agent_kit.py -v`
Expected: PASS (all green).

- [ ] **Step 2: Run the full suite to confirm no regressions**

Run: `.venv/bin/pytest tests/ --tb=short -n auto -q`
Expected: PASS (same baseline as before this slice). If failures appear, confirm
they reproduce on a clean checkout (`git stash`) before attributing them here.

- [ ] **Step 3: Add a CHANGELOG bullet**

Under `## [Unreleased]` in `CHANGELOG.md`, add (create an `### Added` group if absent):

```markdown
### Added
- Dev-agent kit (review core): `CONTRIBUTING.md` sync-map, parity reviewer, review consolidator, and the `/agnes-review` scope-gated review-team command, guarded by `tests/test_contributing_sync_map.py` + `tests/test_dev_agent_kit.py`.
```

- [ ] **Step 4: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): dev-agent kit review core"
```

---

## Self-review notes

- **Spec coverage (slice A):** sync-map → Task 1; parity reviewer → Task 2;
  consolidator → Task 3; `/agnes-review` Team → Task 4; existing-reviewer wiring →
  Task 5; testing strategy (§11 sync-map guard + structural guard) → Tasks 1–5;
  CHANGELOG (sync-map row) → Task 6. Slices B–E are out of scope for this plan.
- **No placeholders:** every code/markdown step shows full content.
- **Type/name consistency:** agent names (`agnes-reviewer-parity`,
  `agnes-review-consolidator`) and helper names (`read_frontmatter`,
  `agent_names`, `REFERENCED_PATHS`) are identical across all tasks; the command
  roster in Task 4 matches the agents created in Tasks 2–3 plus the three existing
  reviewers.
- **Out of scope (later slices):** quality hook (B), builder + `agnes-conventions`
  (C), router section + thin/fat refactor (D), build team (E).
