# Agnes Dev-Agent Kit — Slice D (Router + thin/fat) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Refresh the stale "Specialized agents and skills" section of `CLAUDE.md` into a router that lists the whole dev-agent kit (review team, builder, conventions skill, command, sync-map, quality hook), guarded by a freshness test.

**Architecture:** The existing CLAUDE.md section predates the kit — it lists only the original 4 reviewers/skills. Rewrite it into a "which to use when" routing table + a refreshed inventory, with pointers to `CONTRIBUTING.md` (sync-map) and the quality hook. A guard test asserts the router names the current kit components so it can't silently go stale again. **Thin/fat refactor decision:** the existing four skills (`agnes-{orchestrator,rbac,connectors,release-process}`, 64–86 lines) are already thin; the SKILL.md + references pattern was realized in `agnes-conventions` (slice C). No split of the existing skills is warranted now (YAGNI) — this slice is the router only.

**Tech Stack:** Markdown (CLAUDE.md), pytest (freshness guard).

Spec: `docs/superpowers/specs/2026-06-05-agnes-dev-agent-kit-design.md` §6, §10.

---

## File Structure

- Modify: `CLAUDE.md` — rewrite the `## Specialized agents and skills` section.
- Modify: `tests/test_dev_agent_kit.py` — append a router-freshness guard.
- Modify: `CHANGELOG.md`.

---

## Task 1: Router section + freshness guard

**Files:**
- Modify: `tests/test_dev_agent_kit.py` (append)
- Modify: `CLAUDE.md`

- [ ] **Step 1: Append the failing test**

Append to the END of `tests/test_dev_agent_kit.py`:

```python
def test_claude_md_router_lists_kit_components():
    text = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    required = [
        "/agnes-review",
        "agnes-builder",
        "agnes-conventions",
        "agnes-reviewer-parity",
        "agnes-review-consolidator",
        "CONTRIBUTING.md",
        "post-edit-quality.sh",
    ]
    missing = [t for t in required if t not in text]
    assert not missing, f"CLAUDE.md router must mention kit components: {missing}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_dev_agent_kit.py::test_claude_md_router_lists_kit_components -v`
Expected: FAIL — the stale section omits the new components.

- [ ] **Step 3: Replace the CLAUDE.md section**

In `CLAUDE.md`, replace the entire current section (from the heading
`## Specialized agents and skills` through the line
`Design rationale: \`docs/superpowers/specs/2026-05-15-agnes-agents-design.md\`.`,
i.e. up to but NOT including the next `## Project conventions` heading) with:

```markdown
## Specialized agents, skills & commands

Agnes ships a Claude Code dev-agent kit under `.claude/` (auto-discovered). Pick
the right tool:

| Need | Use | How |
|---|---|---|
| Review a change before merge | `/agnes-review` | scope-gated review **team** (rules / architecture / rbac / parity — only the in-scope subset fires) + `agnes-review-consolidator` → one advisory report (`file:line` + severity, ≤15 findings). Read-only working tree; optional comment-only PR post. |
| Implement a feature (connector / endpoint / web page / repo method / migration) | `agnes-builder` | disciplined implementer (TDD-first, DuckDB↔PG parity in the same change, migration-ladder sync, CHANGELOG, vendor-agnostic, scope discipline). Routes to the `agnes-conventions` playbooks. |
| Cut a release / tag | `agnes-releaser` | per the release process. |
| Deep knowledge while editing a subsystem | `agnes-*` knowledge skills | auto-loaded by description. |

**Agents** (`.claude/agents/`): `agnes-reviewer-{rules,architecture,rbac,parity}`
+ `agnes-review-consolidator` (the review team), `agnes-builder` (implementer),
`agnes-releaser` (release).

**Commands** (`.claude/commands/`): `/agnes-review`.

**Skills** (`.claude/skills/`): knowledge — `agnes-orchestrator`, `agnes-rbac`,
`agnes-connectors`, `agnes-release-process`; implementation playbooks —
`agnes-conventions` (`SKILL.md` + `references/{connector,repo-parity,migration,endpoint-rbac,web-page}.md`).
Read the relevant one before editing that part of the codebase.

**Invariants & guards:** the change-safety **sync-map** lives in `CONTRIBUTING.md`
(walked by the review team — surfaces that must change together, incl. DuckDB↔PG
parity and REST×CLI×MCP coverage). A PostToolUse **quality hook**
(`scripts/post-edit-quality.sh`, wired in `.claude/settings.json`) runs ruff
fix/format + mypy on every edited Python file.

Design rationale: `docs/superpowers/specs/2026-05-15-agnes-agents-design.md`,
`docs/superpowers/specs/2026-06-05-agnes-dev-agent-kit-design.md`.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_dev_agent_kit.py -v`
Expected: PASS (all green, incl. the new freshness guard).

- [ ] **Step 5: Commit**

```bash
git add tests/test_dev_agent_kit.py CLAUDE.md
git commit -m "feat(dev-kit): router section for the dev-agent kit in CLAUDE.md"
```

---

## Task 2: Full-suite check + CHANGELOG

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Kit tests**

Run: `.venv/bin/pytest tests/test_dev_agent_kit.py tests/test_contributing_sync_map.py -v`
Expected: PASS.

- [ ] **Step 2: Full suite**

Run: `.venv/bin/pytest tests/ --tb=short -n auto -q`
Expected: baseline (this slice edits only `CLAUDE.md` + a test; no runtime code).
Known flaky/pre-existing cases under `-n auto` re-pass in isolation — only flag a
NEW failure in a file this slice touched.

- [ ] **Step 3: CHANGELOG bullet**

Under `## [Unreleased]` → `### Added` in `CHANGELOG.md`:

```markdown
- Dev-agent kit (router): `CLAUDE.md` now has a "Specialized agents, skills & commands" routing table covering `/agnes-review`, `agnes-builder`, `agnes-conventions`, the review team, the `CONTRIBUTING.md` sync-map, and the quality hook.
```

- [ ] **Step 4: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): dev-agent kit router section"
```

---

## Self-review notes

- **Spec coverage (§10):** router table → Task 1; (§6 thin/fat) → explicit no-op
  decision (existing skills already thin; agnes-conventions realized the pattern).
- **Freshness guard:** the new test pins the router to the current kit components
  so the section can't silently drift stale again (the failure mode this slice fixes).
- **No placeholders:** full section content + test in the steps.
- **Out of scope:** build team (E), structural API-coverage gate (F).
```
