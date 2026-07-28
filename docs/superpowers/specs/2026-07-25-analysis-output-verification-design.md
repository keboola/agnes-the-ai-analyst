# Verifying analysis output, not just code

## Context

The dev-agent kit already applies verification loops to *code changes*: a
change is checked against deterministic guards before it is claimed done
(`scripts/verify_syncmap.py`, the `verify-agnes-change` skill, the `tests/`
ratchets). This spec asks the parallel question for the *other* agent surface
Agnes ships — the analyst querying data through the CLI rails in `CLAUDE.md`
(`## Querying Agnes data — agent rails`).

Those rails are prose today: "NEVER write `SELECT *` blindly", "ALWAYS include a
`--where` for remote tables", "before computing any business metric, look up the
canonical definition". Prose is exactly the category the verification-loop idea
targets — "anything you keep having to enforce by hand". The question is whether
each rail should become a verification loop, a guard inside the tool, or stay
advice.

Agnes has one advantage a generic Claude Code setup does not: **it is a server.**
It owns `metric_definitions`, `table_registry`, `sync_state`, and it already
receives every analyst session transcript via `agnes push` on `SessionEnd`
(redacted, private-filtered). So the deterministic half of a check can live
server-side, and the distribution channel (marketplace → RBAC → `agnes update`)
already exists.

## The finding that sizes this work

An inventory of the rails against what the CLI and server *already* enforce
changes the shape of the project. Most rails do not want a loop:

| Rail (`CLAUDE.md`) | Enforced today | Decidable mechanically? | Disposition |
|---|---|---|---|
| Remote scan cost | **guard-in-tool** — server 5 GiB cap → `remote_scan_too_large` (`app/api/query.py`) | — | keep |
| Choose right tool (`query_mode`) | **guard-in-tool** — `--scope auto` fallback | — | keep |
| Remote table without `--where` | advisory (`--where` optional, `cli/commands/snapshot.py`) | yes, fully | **promote to guard-in-tool** |
| No implicit `SELECT *` | advisory | yes, fully | **promote to guard-in-tool** |
| Never invent a metric calc | prose only | **half** — session shows whether `catalog --metrics --show` ran, not whether the number used it | weak `Stop`-hook WARN |
| Discovery first (schema before query) | prose only | half — false-positive if discovery happened in a prior session | `Stop`-hook WARN, low priority |
| BigQuery SQL flavor in `--where` | prose only | no | keep as prose — BQ rejects a wrong flavor itself |
| Snapshot hygiene (reuse, drop) | prose only | no (tidiness, not correctness) | keep as prose |

Two rails are already guarded. Two are cheap promotions to a tool guard. Two are
genuinely just advice (the tool or BQ fails loudly on its own). Exactly **one**
(metric usage) is a real verification-loop candidate — and even that is only
half-decidable.

The lesson mirrors the code side: **a guard inside the tool beats a loop wherever
the tool can refuse the bad path.** A loop makes the agent do something, notice it
was wrong, and redo it; a guard stops it happening. The 5 GiB scan cap is not a
loop — it is a tool guard, and it is the most reliable rail we have.

## Non-goals

- **A general "analysis verification framework."** The inventory does not support
  one. Phase 1 is two small tool guards plus one optional advisory hook.
- **Judging whether an answer is *correct*.** Whether a number in prose actually
  came from the canonical metric SQL is not decidable from tool calls; only an
  LLM judge could attempt it, and that is a per-answer cost on every session. Out
  of scope — do not turn a linter into a reviewer.
- **Blocking analysts.** The guards refuse a *fetch shape*, never a question.

## Phase 1 — help the analyst (client-side)

Ordered cheapest-first, same discipline as the code loop.

### 1a. `agnes snapshot create` refuses an unbounded remote fetch

On a `query_mode='remote'` table, require either `--where` or `--limit`. This is
the "ALWAYS include a `--where` for remote tables" rail made mechanical, at the
one place the fetch is actually issued.

- **Severity by cost, not blanket-BLOCKING.** If the pre-fetch estimate is
  available and under a threshold (proposed 1 GiB scan), a missing predicate is a
  WARN — a legitimate small fetch is not worth an error. At or above the
  threshold, or when no estimate is available, it is BLOCKING with the standard
  next-step hint (`cli/query_hints.py`): add a `--where`, or pass `--limit`.
- Deterministic, unit-testable exactly like the sync-map detectors.

### 1b. `agnes snapshot create` rejects implicit `SELECT *` on remote

Require an explicit `--select` column list on remote tables; reject `*` or an
absent list. This is "ALWAYS list specific columns" made mechanical. BLOCKING —
there is no cheap-fetch exception for `SELECT *` at 225M-row scale.

Both 1a and 1b are guards *in the tool*, not a loop: the analyst's agent cannot
issue the wasteful fetch, so there is nothing to detect after the fact.

### 1c. (optional) A `Stop`-hook metric-usage WARN

`agnes init` already manages the analyst workspace's `.claude/settings.json`
hooks (`SessionStart`, `SessionEnd`). Add a third, `Stop`, that runs a
deterministic check over the session's tool calls and returns a WARN to the agent
before the answer reaches the human:

> This answer reports a metric-shaped figure but no `catalog --metrics --show`
> ran this session. Confirm it matches the canonical definition, or look it up.

- **WARN only, never BLOCKING.** The check can see that a lookup did *not* happen;
  it cannot see that the number is wrong. A false positive (analyst computed a
  non-metric aggregate) must not block the answer.
- The `SessionStart` `agnes update` re-asserts Agnes-owned hooks, so this is not a
  pure honor system — but it is defense-in-depth, not enforcement.

## Phase 2 — a record of what was done (server-side) — deferred

The transcripts are already on the server. A post-hoc sweep could run the same
checks over uploaded sessions and record where a rail was skipped. That is
strictly more capable than the client hook (no trust in the client, full DB
access) and feeds the signal Knowledge Flywheel wants to collect.

But it is a **different kind of decision.** The client-side phase *helps* the
analyst; a server-side sweep *measures* them. It reports rather than repairs — the
answer already reached the human. Measuring people's work is a governance choice
with its own stakeholders, not a technical guard to bundle into Phase 1. This
spec records the option and deliberately does not recommend it as a first step.

If pursued, open questions for its own spec: what is retained vs. computed-and-
discarded; who sees the report; whether an analyst can see their own; how it
interacts with the private-session filter that already exempts transcripts from
upload.

## Enforcement reality

Phase 1a/1b are `cli/commands/snapshot.py` changes with unit tests — the same
shape as the existing rails, guarded by the same kind of test. 1c is a hook
addition in `cli/lib/hooks.py` plus a small deterministic checker; its logic is
unit-testable offline against a recorded transcript, mirroring how
`test_route_auth_guard.py` walks a static structure rather than a live run.

No new backend, no migration, no RBAC surface. The distribution path
(marketplace → `agnes update`) and the transcript channel (`agnes push`) already
exist.

## Framing

The one-line version: **"governed data access" extends to "governed answers"** —
but the inventory shows that for most rails the governance already belongs in the
tool, not in a loop bolted on afterward. The verification loop is the right tool
for exactly the residue the tool guard cannot cover, and forcing it onto rails a
guard handles better would repeat the mistake the code-side work was built to
avoid.
