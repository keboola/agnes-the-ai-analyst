# Skill Guidelines

This document outlines best practices for writing skills and tools for inclusion in the Agnes marketplace. The guidelines help skills integrate smoothly, remain maintainable, and serve clear use cases without duplication.

## What belongs in a skill?

A skill is a reusable, discoverable, shareable unit of functionality—a command, a workflow pattern, a domain playbook, or a knowledge base. Before authoring a skill, ensure it meets these fundamentals:

### One purpose per skill

Each skill solves **one clear problem** or teaches **one workflow**. Multi-purpose catch-all skills are harder to discover, harder to maintain, and harder to decide when to invoke. If you're tempted to add "advanced options" that serve multiple distinct audiences, consider whether those deserve a separate skill.

### Description states *when to use it*

The skill description is the first and often only thing a user reads before deciding whether to invoke the skill. It must state the **trigger condition** — when the user should reach for this skill — not a summary of what it does.

**Good:** "Audit a branch before merge — checks test status, code review state, and deploy eligibility against the org's ship-readiness gates."

**Bad:** "Checks tests, reviews, and deploy status."

The first example tells you *when* to use it (before merge). The second describes what it does without context. If the user has already decided to audit a branch, the second description is redundant; if they haven't, it doesn't help them.

Keep descriptions lean — 60–200 characters is typical. The linter warns when descriptions are too short (missing the trigger context) or too long (saying everything instead of pointing to the README). Move detailed rationale, examples, and walkthroughs to the body.

### Keep the body lean; extend with references

The skill body (markdown after the YAML frontmatter) should introduce the core idea, link to or embed detailed steps, and point at reference materials. **Do not paste 3000-word procedural docs into the body.** Users read the description to decide *whether* to invoke; they read the body for context and entry points; they visit the references for deep dives.

Patterns:

- **Playbook skills** (workflows for a team process): a 200–300-word summary of the gates/steps, inline checklist template, links to per-stage playbooks in `references/`.
- **Connector skills** (how to set up a data source): a brief "you need X, here's the high-level flow" intro, then link to the `references/` walkthrough.
- **Tool skills** (how to use a command or API): a quick motivating example, then link to the reference.
- **Knowledge skills** (common patterns, gotchas, design rationale): embed the key insight, link to the fuller explainer in references.

Avoid re-uploading the same 500-word walkthrough as separate skills. If two skills would duplicate a reference, **extend the existing skill** (add a new section, refine the description) rather than create a lookalike.

## Rule catalogue

The skill linter runs checks on uploaded skills to catch common issues and guide authors toward marketplace norms. Findings are labeled with a **rule ID** (`SL###`) and a severity (`info` or `warn`). This section explains what each rule catches and how to fix it.

<a id="sl002"></a>
### SL002 — bloat

**Fires when:** skill body exceeds the configured character limit (default: 8000 characters).

**Why it matters:** skill bodies that balloon past 8000 characters (roughly 3–4 pages of dense text) are hard to parse for users making snap decisions about invocation. It signals the skill is trying to do too much, the explanation is over-detailed, or reference material belongs in a separate doc.

**How to fix:**
1. Check that the skill **has one clear purpose**. If the body lists multiple independent workflows, split into separate skills.
2. Move procedural detail to `references/` — the body should motivate and link, not replicate the full walkthrough.
3. Trim examples to one or two representative cases; drop edge cases into the references.
4. Replace inline code blocks with links to repos or commands.

If your skill legitimately needs >8000 characters, the operator can raise the threshold via instance config (`guardrails.lint_max_body_chars`).

<a id="sl010"></a>
### SL010 — craft review

**Fires when:** the LLM reviewer detects signs of sloppy authoring — incomplete sentences, stubs, placeholder text, or orphaned sections.

**Why it matters:** skills are not drafts; they're published, discoverable assets. Users expect them to be thoughtful, correct, and complete. A skill with "TODO: finish this part" or "need to add examples" signals that the marketplace has lower-than-expected bars.

**How to fix:**
1. Read the skill description, body, and any references aloud to yourself. Does it flow? Are all sentences complete?
2. Check that no section ends with "…" or a trailing thought.
3. Verify all code examples run (or at least look syntactically valid).
4. Ensure the description is not a placeholder like "Tool for X" or "Skill about Y".

<a id="sl011"></a>
### SL011 — trigger phrase (degraded)

**Fires when:** a skill is re-audited within a short interval (default: 144 hours = 6 days) and the linter detects a possible pattern of spammy re-uploads without substantive changes.

**Why it matters:** the marketplace should reflect stable, well-considered contributions, not churn. Constant micro-updates suggest the author is unsure of the skill's direction or is using the marketplace as a dumping ground for half-baked ideas.

**How to fix:**
1. **Plan before uploading.** Finalize the skill description, body, and structure locally before the first upload.
2. **Batch updates.** If you discover issues after upload, fix multiple problems in one cohesive update rather than re-uploading three times in a row.
3. **Wait before re-auditing.** If the linter suggests waiting, respect the interval. The grace period gives you time to think and plan the next version.

This rule is marked *degraded* because the heuristic can misfire on legitimate rapid refinement. If you have a strong reason for frequent updates, mention it in the PR or upload note.

<a id="sl012"></a>
### SL012 — duplicate candidates (degraded)

**Fires when:** the new skill's description is very similar to one of the top N (default: 5) most recent skills in the marketplace, suggesting a possible unintentional near-duplicate.

**Why it matters:** duplicate skills confuse users (which one should I use?), split maintenance burden, and crowd the marketplace. Most duplicates are accidental — the author didn't realize a similar skill already existed.

**How to fix:**
1. **Check the marketplace** before writing a new skill. Use `/marketplace search <keywords>` or search by category. If a similar skill exists, consider **extending it** instead — add a new section, refine the description, update the references.
2. **If the new skill is genuinely different**, update the description to highlight what sets it apart. "Like X but focuses on Y" or "Companion to X for Z workflow" makes the distinction clear.
3. **Coordinate with the author** of the existing skill if it's team-maintained. A shared roadmap beats competing versions.

This rule is marked *degraded* because the description-similarity heuristic can flag skills that happen to use the same keywords but serve different purposes. If the linter warns but your skill is distinct, the warning is a signal to strengthen the description's trigger-context or uniqueness statement, not necessarily to delete the skill.

---

## Linter configuration

Operators can tune linter thresholds via instance config (`guardrails` block):

- `lint_max_body_chars` (default: 8000) — character limit for SL002 bloat warnings
- `lint_duplicate_top_n` (default: 5) — how many recent skills to check for SL012 duplicates
- `lint_audit_min_interval_hours` (default: 144) — grace period for SL011 rapid-update pattern

See `config/instance.yaml.example` for examples.
