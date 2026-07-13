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

**Fires when:** a single holistic LLM pass judges the skill on three axes and finds it wanting on at least one:

- **Trigger clarity** — the description doesn't state *when* to invoke the skill (see "Description states when to use it" above). The finding includes the model's suggested one-sentence rewrite.
- **Single purpose** — the skill bundles multiple unrelated capabilities instead of solving one clear problem.
- **Confirmed duplicate** — the model reviewed the lexical near-duplicate candidates (SL012's shortlist) against the skill's actual purpose and confirmed at least one is a genuine, substantive duplicate — not just sharing vocabulary.

**Why it matters:** these are judgment calls a regex or keyword search can't make reliably. SL010 is the substantive layer on top of the mechanical checks (SL002, and the `QC-*` quality-check findings for placeholder text, TODOs, and too-short descriptions/docs).

**How to fix:**
1. **Trigger clarity:** paste in the suggested rewrite, or write your own sentence naming the trigger condition.
2. **Single purpose:** split the skill along its distinct capabilities, or narrow the description to the one it's actually for.
3. **Confirmed duplicate:** follow the SL012 guidance below — extend the existing skill instead of publishing a near-copy, or sharpen the description to make the real distinction obvious.

When the LLM reviewer is unavailable (no configured API key, or the guardrails LLM provider isn't ready), SL010 doesn't run and the linter falls back to the degraded-mode SL011/SL012 heuristics below instead.

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
