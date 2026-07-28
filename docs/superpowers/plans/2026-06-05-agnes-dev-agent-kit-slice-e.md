# Agnes Dev-Agent Kit — Slice E (Build Team) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** A `/agnes-build` command that decomposes a plan into independent tasks (respecting sync-map coupling), implements them in parallel git worktrees via `agnes-builder`, integrates the results, and auto-runs `/agnes-review`.

**Architecture:** Three new prose artifacts — `agnes-decomposer` (plan + sync-map coupling → independent task list), `agnes-integrator` (collect worktree diffs → serialized merge), and the `/agnes-build` command that orchestrates them as a Team. The implementers are `agnes-builder` instances (slice C) run with `isolation:'worktree'`. Like slice A, these are guidance prose validated by structural guards (frontmatter + cross-references) and by use — there is no runtime code. The command documents the real mechanism AND its limitations honestly (cross-worktree diff integration is non-trivial; the integrator owns it and stops on unresolvable conflicts).

**Tech Stack:** Claude Code agent/command markdown, Team API (`TeamCreate`/`TaskCreate`/`TeamDelete`), `Agent`/`Task` tool with `isolation:'worktree'`, pytest (structural guards).

Spec: `docs/superpowers/specs/2026-06-05-agnes-dev-agent-kit-design.md` §7.

---

## File Structure

- Create: `.claude/agents/agnes-decomposer.md`
- Create: `.claude/agents/agnes-integrator.md`
- Create: `.claude/commands/agnes-build.md`
- Modify: `tests/test_dev_agent_kit.py` (append structural guards)
- Modify: `CHANGELOG.md`

---

## Task 1: Decomposer + integrator agents

**Files:**
- Modify: `tests/test_dev_agent_kit.py` (append)
- Create: `.claude/agents/agnes-decomposer.md`
- Create: `.claude/agents/agnes-integrator.md`

- [ ] **Step 1: Append the failing tests**

Append to the END of `tests/test_dev_agent_kit.py`:

```python
@pytest.mark.parametrize("agent", ["agnes-decomposer", "agnes-integrator"])
def test_buildteam_agents_have_valid_frontmatter(agent):
    path = AGENTS_DIR / f"{agent}.md"
    assert path.exists(), f"{agent}.md must exist"
    fm = read_frontmatter(path)
    assert fm.get("name") == agent, "name must match filename"
    assert "description" in fm, "agent must declare a description"
    assert "tools" in fm, "agent must declare its tools"


def test_decomposer_uses_sync_map_coupling():
    text = (AGENTS_DIR / "agnes-decomposer.md").read_text(encoding="utf-8")
    assert "CONTRIBUTING.md" in text, "decomposer must read the sync-map"
    assert "parity" in text.lower() and "migration" in text.lower(), (
        "decomposer must keep parity siblings + migration steps coupled"
    )


def test_integrator_serializes_migrations():
    text = (AGENTS_DIR / "agnes-integrator.md").read_text(encoding="utf-8")
    assert "migration" in text.lower(), "integrator must handle the migration task"
    assert "worktree" in text.lower(), "integrator must collect worktree diffs"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_dev_agent_kit.py -k "buildteam or decomposer or integrator" -v`
Expected: FAIL — `agnes-decomposer.md must exist`.

- [ ] **Step 3: Create `.claude/agents/agnes-decomposer.md`**

```markdown
---
name: agnes-decomposer
description: Splits an implementation plan into INDEPENDENT tasks for parallel build, respecting the CONTRIBUTING.md sync-map coupling rules (parity siblings, migration steps, and merge-magnet files stay coupled). Read-only — emits a task graph, writes no code. Used by /agnes-build.
tools: Read, Grep, Bash
model: sonnet
---

You turn a plan into a task graph the build team can run in parallel WITHOUT
conflicts. Read-only: you analyze and emit JSON; you never edit code.

## Inputs

A writing-plans plan (path passed by the parent) or a task description, plus the
`CONTRIBUTING.md` sync-map. Read both first.

## Coupling rules (NEVER split these across tasks)

Derived from the sync-map — surfaces that must change together go in ONE task:

1. **Parity siblings** — `src/repositories/X.py` + `src/repositories/X_pg.py` +
   their `src/repositories/__init__.py` `_REGISTRY` registration + the
   `tests/db_pg/test_<cluster>_contract.py` case. One task.
2. **A migration step** — the `src/db.py` `_vN_to_v(N+1)` + the
   `migrations/versions/*` revision + `src/db_pg.py` `Base.metadata`. One task.
   And **at most ONE migration task per build run** — the `SCHEMA_VERSION` ladder
   is serial; two parallel migrations would collide on the version number.
3. **ResourceType + Spec** — a new `ResourceType` enum value + its
   `ResourceTypeSpec` registration in `app/resource_types.py`. One task.
4. **An endpoint + its CLI + MCP coverage** (per the sync-map's API-coverage row)
   — one task, so the three surfaces land together.

## Merge magnets (do NOT let implementers edit these in parallel)

`CHANGELOG.md` and `CLAUDE.md` collide if edited in parallel worktrees. Mark them
in EVERY task as "emit bullet via structured output" — the integrator folds all
bullets in one pass. Implementers must NOT edit these files directly.

## Output (JSON only)

    {"tasks": [
       {"id": "t1", "title": "...", "files": ["..."],
        "depends_on": [], "is_migration": false,
        "changelog_bullet_hint": "...", "claude_md_note": null}
     ],
     "migration_task_id": "t3" | null,
     "notes": "anything that could not be cleanly parallelized → keep in one task"}

If two candidate tasks share a file (other than a merge magnet), MERGE them — a
shared file means a conflict risk. When in doubt, fewer, coarser tasks beat racy
fine-grained ones. Verify file overlaps with real `grep`/`git` inspection.
```

- [ ] **Step 4: Create `.claude/agents/agnes-integrator.md`**

```markdown
---
name: agnes-integrator
description: Integrates the build team's parallel worktree results into one coherent change — applies tasks in dependency order, serializes the single migration task last, folds CHANGELOG/CLAUDE.md bullets in one pass, and stops on any unresolvable conflict. Used by /agnes-build.
tools: Read, Bash, Edit, Grep, Glob
model: sonnet
---

You merge the build team's worktree results into the target branch. Each
implementer committed its task inside its own git worktree; your job is to bring
those commits together cleanly.

## Inputs

The decomposer's task graph + the list of implementer worktrees (path + branch +
commit per task), passed by the parent.

## Procedure

1. **Order.** Topologically sort tasks by `depends_on`. The `migration_task_id`
   (if any) is applied **LAST** — the `SCHEMA_VERSION` ladder is serial, so a
   migration must land after all other schema-touching work.
2. **Bring in each task** in order. For each worktree, apply its commits to the
   target branch (e.g. `git cherry-pick <range>` or `git merge --no-ff` the task
   branch). A clean apply → continue.
3. **Merge magnets.** Implementers did NOT edit `CHANGELOG.md` / `CLAUDE.md`.
   Collect their `changelog_bullet_hint` / `claude_md_note` and apply ALL of them
   in a single edit each, under `## [Unreleased]`.
4. **Conflicts.** A conflict that is a trivial merge magnet (both added a bullet)
   → resolve by keeping both. ANY other conflict (same code region from two tasks)
   → STOP, leave the tree clean (abort the cherry-pick/merge), and report which
   two tasks collided and on which file — the decomposer should have coupled them.
   Do NOT force a resolution you are unsure about.
5. **Verify.** After integration, run `.venv/bin/pytest tests/ --tb=short -n auto -q`
   once. Report the result.

## Output

A report: tasks integrated (in order), migration applied (yes/which/last), bullets
folded, any conflict that stopped you (with the two task ids + file), and the
post-integration test result. The parent runs `/agnes-review` on the unified diff
next — you do not.
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_dev_agent_kit.py -k "buildteam or decomposer or integrator" -v`
Expected: PASS (4 cases: 2 frontmatter + decomposer + integrator).

- [ ] **Step 6: Commit**

```bash
git add tests/test_dev_agent_kit.py .claude/agents/agnes-decomposer.md .claude/agents/agnes-integrator.md
git commit -m "feat(dev-kit): add build-team decomposer + integrator agents"
```

---

## Task 2: `/agnes-build` command

**Files:**
- Modify: `tests/test_dev_agent_kit.py` (append)
- Create: `.claude/commands/agnes-build.md`

- [ ] **Step 1: Append the failing test**

Append to the END of `tests/test_dev_agent_kit.py`:

```python
def test_agnes_build_command_references_only_existing_agents():
    path = COMMANDS_DIR / "agnes-build.md"
    assert path.exists(), "agnes-build.md command must exist"
    fm = read_frontmatter(path)
    assert "description" in fm, "command must declare a description"
    assert "allowed-tools" in fm, "command must declare allowed-tools"
    text = path.read_text(encoding="utf-8")
    referenced = set(re.findall(r"agnes-[\w-]+", text))
    # command + skill names that are not agents:
    referenced -= {"agnes-build", "agnes-review", "agnes-conventions"}
    unknown = sorted(referenced - agent_names())
    assert not unknown, f"agnes-build references unknown agents: {unknown}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_dev_agent_kit.py -k agnes_build_command -v`
Expected: FAIL — `agnes-build.md command must exist`.

- [ ] **Step 3: Create `.claude/commands/agnes-build.md`**

````markdown
---
description: Build a plan in parallel. Decomposes a writing-plans plan into independent tasks (respecting sync-map coupling), implements each in its own git worktree via agnes-builder, integrates the results (migration serialized last), then runs /agnes-review on the unified diff. For genuinely entangled work it falls back to fewer/coarser tasks.
allowed-tools: Task, Bash, Read, Grep, Glob, TeamCreate, TeamDelete, TaskCreate, TaskUpdate, TaskList, TaskGet, SendMessage
argument-hint: "<path to a writing-plans plan, or a task description>"
---

# /agnes-build — parallel build team

Orchestrate a parallel implementation. `$ARGUMENTS` is a plan path or a task
description.

## 1. Decompose

Spawn `agnes-decomposer` (`subagent_type=agnes-decomposer`) with the plan/description
+ a pointer to `CONTRIBUTING.md`. It returns a JSON task graph (independent tasks,
`migration_task_id`, merge-magnet bullets handled out-of-band).

If the decomposer returns a single task (work didn't parallelize), just run it
inline via `agnes-builder` and skip the team — don't pay orchestration overhead.

## 2. Build in parallel worktrees

```
TeamCreate(team_name="agnes-build", description="Parallel build")
```

For each task, spawn an `agnes-builder` via the Task tool with
`subagent_type=agnes-builder`, `team_name="agnes-build"`, `isolation="worktree"`,
`run_in_background=true`. Prompt = the task's title + files + the rule "commit your
work in this worktree; do NOT edit CHANGELOG.md or CLAUDE.md — emit your bullet in
your report instead." Honor `depends_on` (don't start a task until its deps finish).

**Worktree isolation is what makes this safe** — each builder edits its own copy,
so parallel file writes can't collide. The cost is that the diffs are in separate
worktrees and must be integrated (next step).

## 3. Integrate

When all builders finish, spawn `agnes-integrator`
(`subagent_type=agnes-integrator`) with the task graph + each worktree's
path/branch/commit. It applies tasks in dependency order, lands the migration task
LAST, folds the CHANGELOG/CLAUDE.md bullets, and STOPS on any non-merge-magnet
conflict (reporting the colliding tasks). If it stops, surface that to the user —
the decomposer under-coupled; do not force-merge.

## 4. Review

Run `/agnes-review` on the unified diff (the integrated result on the target
branch). Relay its advisory report.

## 5. Clean up

`TeamDelete(team_name="agnes-build")`. Worktrees that were integrated can be
removed; leave any that the integrator flagged unresolved for the user.

## Honest limitations

- Cross-worktree integration is the hard part; if tasks share non-magnet files the
  integrator will stop rather than guess. Prefer coarser tasks over racy ones.
- At most ONE migration task per run (the `SCHEMA_VERSION` ladder is serial).
- This orchestrates `agnes-builder`, which is itself disciplined (TDD, parity,
  CHANGELOG) — the build team does not relax those rules.
````

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_dev_agent_kit.py -v`
Expected: PASS (all green).

- [ ] **Step 5: Commit**

```bash
git add tests/test_dev_agent_kit.py .claude/commands/agnes-build.md
git commit -m "feat(dev-kit): add /agnes-build parallel build-team command"
```

---

## Task 3: Router update + full-suite + CHANGELOG

**Files:**
- Modify: `CLAUDE.md` (add `/agnes-build` to the router)
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Add `/agnes-build` to the CLAUDE.md router**

In the `## Specialized agents, skills & commands` section of `CLAUDE.md`:
- Add a routing-table row after the `/agnes-review` row:
  `| Implement a whole plan in parallel | \`/agnes-build\` | decomposes a plan into independent tasks (sync-map coupling), builds each in its own git worktree via \`agnes-builder\`, integrates (migration serialized last), then runs \`/agnes-review\`. |`
- In the **Agents** line, add `agnes-decomposer` + `agnes-integrator` (the build team).
- In the **Commands** line, change `\`/agnes-review\`.` to `\`/agnes-review\`, \`/agnes-build\`.`

- [ ] **Step 2: Run the router freshness guard + kit tests**

Run: `.venv/bin/pytest tests/test_dev_agent_kit.py -v`
Expected: PASS (the existing `test_claude_md_router_lists_kit_components` still passes; new agents are present).

- [ ] **Step 3: Full suite**

Run: `.venv/bin/pytest tests/ --tb=short -n auto -q`
Expected: baseline (only markdown + tests changed). Known flaky cases re-pass in
isolation; flag only a NEW failure in a touched file.

- [ ] **Step 4: CHANGELOG bullet**

Under `## [Unreleased]` → `### Added` in `CHANGELOG.md`:

```markdown
- Dev-agent kit (build team): `/agnes-build` decomposes a plan into independent tasks (sync-map coupling), implements each in a parallel git worktree via `agnes-builder`, integrates them (migration serialized last, merge-magnet bullets folded), and runs `/agnes-review` — via new `agnes-decomposer` + `agnes-integrator` agents.
```

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md CHANGELOG.md
git commit -m "feat(dev-kit): wire /agnes-build into the router + changelog"
```

---

## Self-review notes

- **Spec coverage (§7):** decomposer (coupling) → Task 1; integrator (serialized
  migration + merge magnets + stop-on-conflict) → Task 1; `/agnes-build`
  orchestration (decompose → worktree build → integrate → review) → Task 2; router
  → Task 3.
- **Honesty:** the command documents the real limitation (cross-worktree
  integration is hard; integrator stops rather than force-merge) per the spec's
  risk section — no silent capability claims.
- **Cross-reference guard:** the new test discards command/skill names
  (`agnes-build`, `agnes-review`, `agnes-conventions`) and asserts every remaining
  `agnes-*` token is a real agent — catches a typo'd `subagent_type`.
- **No placeholders:** full agent/command content in the steps.
- **Out of scope:** structural API-coverage gate (F).
```
