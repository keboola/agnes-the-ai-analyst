---
name: agnes-wayfinder
description: Chart a large, foggy effort as a map of decision tickets in docs/superpowers/maps/, then resolve them one per session until a plan can be written. Use only when the user explicitly asks to wayfind or map an effort — the destination is known but too many decisions are open to write a plan yet.
disable-model-invocation: true
allowed-tools: Bash, Read, Write, Edit, Grep, Glob
---

# agnes-wayfinder

For work where the **destination is known but the route isn't**, and the route is
too long to hold in one session. Not a build process — this produces **decisions**,
and hands off to `superpowers:writing-plans` the moment there are none left.

Adapted from Matt Pocock's `wayfinder` skill (MIT) — rewired to this repo's
tooling, and to plain markdown instead of an issue tracker.

## When NOT to use this

Run the test before charting anything: **can you already state the steps?**

- **You can** → you have no fog. Go straight to `superpowers:writing-plans`, then
  `/agnes-build`. A map here is pure overhead.
- **One session's worth of work** → just do it.
- **A known bug** → `superpowers:systematic-debugging`.
- **A conflicted branch, a rebase, a migration with a clear endpoint** → these
  have obvious destinations and no fog. Not maps.

The signal for a map is that you cannot write step 3 because you have not decided
step 1 — and step 1 needs a conversation, an experiment, or a fact you don't have.

## The map is markdown, not GitHub issues

```
docs/superpowers/maps/<effort-slug>/
├── map.md                       # the index
└── issues/NN-<slug>.md          # one decision per file, numbered from 01
```

**Committed, not scratch.** `docs/brainstorms/` is gitignored and has lost a
punch-list to a discarded checkout before; a map that dies with a worktree is
worse than no map.

**Never GitHub issues.** This repo's *Issue economy* convention (`CLAUDE.md`) says
fix or close, don't spawn — and it is right: a wayfinder map would otherwise dump
twenty open questions into a public tracker. Map tickets are working notes with a
lifespan of one effort, so they stay in the repo. The convention is not being
bent here; it simply doesn't apply to files.

Consequence to respect: the map is public. Same rule as everything else in this
repo — no customer-specific content, no internal hostnames, no cross-references
to private repos. Frame motivations abstractly.

## map.md

The whole effort at low resolution. Loaded once per session. Open tickets are
**not** listed here — they are files, found by the frontier query.

```markdown
# Map: <effort name>

## Destination

<what reaching the end looks like — the spec, decision, or change this is finding
its way to. One or two lines. Every session orients to it before picking a ticket.>

## Notes

<domain; skills every session should consult; who else holds answers; standing
preferences for this effort>

## Decisions so far

<!-- index only: one line per resolved ticket, then follow the link for detail -->

- [NN — <ticket title>](issues/NN-<slug>.md) — <one-line gist of the answer>

## Not yet specified

<!-- in-scope fog: questions you can see coming but cannot phrase sharply yet -->

## Out of scope

<!-- consciously ruled beyond the destination; never graduates -->
```

The map is an **index, not a store**. A decision lives in exactly one place — its
ticket. The map gists it and links; it never restates it.

## Tickets

```markdown
# NN — <the question, as a question>

Type: research | prototype | grilling | task
Status: open | claimed | resolved
Blocked by: NN, NN   (or —)

## Question

<the decision this ticket resolves, sized to one session>
```

Resolve by appending `## Answer`, flipping `Status: resolved`, and appending the
one-line gist to the map's *Decisions so far*. Never paste artifacts into the
ticket — link them.

**Types, and what resolves them here:**

| Type | Resolved by | Human needed |
|---|---|---|
| `research` | reading code/docs — `Grep`, `Glob`, `gh`, the `agnes-*` knowledge skills | no |
| `task` | doing the unblocking chore — provisioning, moving data, running a migration on a dev box | sometimes |
| `grilling` | `superpowers:brainstorming` — one question at a time | **yes** |
| `prototype` | a cheap throwaway artifact to react to (`frontend-design` for UI) | **yes** |

**A `grilling` or `prototype` ticket cannot be resolved by the agent alone.** An
agent that answers its own design questions has produced a hallucinated decision
wearing the costume of a resolved ticket — the single worst failure mode of this
skill. If the human isn't available, leave it open and take another.

## Frontier

Open, unblocked, unclaimed — lowest number first:

```bash
for f in docs/superpowers/maps/<effort>/issues/*.md; do
  grep -q '^Status: open' "$f" || continue
  b=$(sed -n 's/^Blocked by: //p' "$f" | head -1)
  [ "$b" = "—" ] && { echo "$f"; continue; }
  blocked=0
  for n in ${b//,/ }; do
    grep -q '^Status: resolved' docs/superpowers/maps/<effort>/issues/$n-*.md || blocked=1
  done
  [ $blocked = 0 ] && echo "$f"
done
```

## Mode 1 — chart the map

1. **Name the destination.** `superpowers:brainstorming` with the user. The
   destination fixes the scope, so it is settled first and it is theirs, not yours.
2. **Map the frontier.** Brainstorm again, **breadth-first** — fan out across the
   whole space rather than deep on one thread. **If no fog surfaces, stop**: the
   route is already clear, say so and point at `superpowers:writing-plans`.
3. **Write `map.md`** — Destination and Notes filled, Decisions-so-far empty, the
   fog sketched into *Not yet specified*.
4. **Write the tickets you can specify now.** The test for ticket-vs-fog is
   whether you can state the question precisely *now* — not whether you can answer
   it. Wire `Blocked by` in a second pass, once the numbers exist.
5. **Resolve the `research` tickets** — they need no human and their facts often
   reshape the rest of the map. Everything else waits.
6. **Stop.** Charting is one session's work. It resolves no decisions.

## Mode 2 — work through the map

1. Read `map.md` — the low-res view, not every ticket.
2. Take the first frontier ticket (or the one the user named). **Claim it**:
   `Status: claimed`, saved, before any work.
3. Resolve it. Zoom into related tickets on demand; use the skills *Notes* names.
4. Record: `## Answer`, `Status: resolved`, gist appended to *Decisions so far*.
5. Update the map — add newly-surfaced tickets, graduate fog that just became
   sharp (deleting it from *Not yet specified*, so it lives only as its ticket),
   and if the answer reveals a ticket sits past the destination, **rule it out of
   scope**: resolve nothing, move one line to *Out of scope*, and say why.

**One ticket per session**, except `research`. The pull to keep going is the
signal to stop and let the next session start from the updated map.

## Exit

When no tickets remain, the map is done and the route is clear. Hand off:

1. `superpowers:writing-plans` — the map's *Decisions so far* is the input.
2. `/agnes-build` — implements the plan in parallel worktrees.
3. `verify-agnes-change` → `/agnes-review` — the usual gates.

Then delete `issues/` and keep `map.md` as the decision record, or promote it to
`docs/superpowers/specs/YYYY-MM-DD-<name>.md` if it earned that.

**The map never becomes the build.** If you catch yourself implementing the
destination inside a ticket, you have reached the edge of the map — that is the
hand-off signal, not a reason to keep typing.
