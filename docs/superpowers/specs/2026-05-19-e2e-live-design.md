# Agnes live E2E walkthrough — design

**Status:** brainstorm (approved by zsrotyr 2026-05-19), not yet implemented
**Date:** 2026-05-19
**Author:** zsrotyr
**Discovery artefacts:** `/tmp/agnes-e2e/run-20260519T054136Z/` (transcript,
3 asciinema casts, 3 mp4 videos, the `FoundryAI` workspace, 3 runner scripts).
[Issue #345](https://github.com/keboola/agnes-the-ai-analyst/issues/345)
captures the 9 items the discovery surfaced.

## Problem

Two questions never get a confident answer on `main`:

1. **Does a fresh-machine install of Agnes still work end-to-end?** Tests cover
   unit + integration + a handful of webapp routes. Nothing exercises the
   full path `paste-prompt → uv tool install → agnes init → catalog → query
   (local) → query --remote → snapshot create`. Regressions in that path land
   in deployments before they land in test runs (we've seen the server-side
   `agnes schema <remote>` HTTP 500 only because an exploratory walkthrough
   caught it).
2. **Does it work for an analyst with Claude Code on the other end?** A real
   analyst doesn't type `agnes query "SELECT …"` — they ask Claude Code "show
   me the top 5 days last week," and Claude Code reaches for `agnes`. The
   only test of that loop today is "we'll find out from the analyst on
   Slack."

A live walkthrough — fresh workspace, headless `claude --print` simulating
the analyst, recorded to asciinema, parsed into a PASS/FAIL report — answers
both questions in one ~3-minute run against any running Agnes instance. We've
done it manually three times during the discovery; what we need is to make
it cheap to do it again on every PR / nightly / pre-release.

## Approach

A custom Claude Code subagent (`agnes-e2e-tester`) is the single entry
point. The main agent invokes it with a list of paste-prompt names; the
subagent orchestrates the four layers internally, calls a small set of
bash primitives for the mechanical work (workspace, asciinema, claude
subprocess, transcript parsing), and returns a markdown report.

```
.claude/agents/agnes-e2e-tester.md   THE AGENT — single interface
  frontmatter: tools: Read, Write, Bash; model: opus
  body: system prompt that orchestrates layers 0-3 against the
        paste-prompts the caller named

scripts/e2e-live/                    INTERNAL PRIMITIVES (no per-layer scripts)
├── _common.sh                       workspace creation + asciinema rec wrapper
├── _claude-run.sh                   `claude --print --session-id ...` invocation
│                                    with consistent flags + budget caps
├── _parse-transcript.py             JSONL transcript → per-layer PASS/FAIL +
│                                    bash-call inventory + "Friction" extraction
├── _mint-analyst.sh                 Layer 0 server calls: create user → set-password
│                                    → group → grants → form-login → mint PAT →
│                                    write `<target>-analyst.md` to the prompt library
├── _cleanup-analyst.sh              revoke PAT + remove from group + delete group +
│                                    deactivate user (called by the agent on teardown)
└── README.md                        how the agent calls these primitives
```

The agent is the orchestrator; the bash primitives do the I/O.

### Paste-prompt naming convention

Two MD files per target, by convention:

```
~/.config/agnes-e2e/prompts/
├── <target>.md              admin paste-prompt (e.g. foundryai-dev.md)
└── <target>-analyst.md      non-admin analyst paste-prompt
                             (Layer 0 creates this from <target>.md)
```

The `-analyst` suffix is how the agent identifies the persona without
parsing the MD body. When the caller says "test foundryai-dev":

1. Agent looks up `~/.config/agnes-e2e/prompts/foundryai-dev.md`.
2. If `~/.config/agnes-e2e/prompts/foundryai-dev-analyst.md` exists,
   agent runs both walkthroughs (admin + analyst) and reports the diff.
3. If only the admin MD exists, agent **auto-runs Layer 0** (admin
   creates a fresh analyst user, mints a PAT, writes the `-analyst.md`)
   before the analyst-persona walkthrough.
4. After both walkthroughs, agent calls `_cleanup-analyst.sh` to revoke
   the Layer-0 user (keeps the prompt library tidy, leaves the audit log).

Cross-target sweeps just take multiple names: "test foundryai-dev,
foundryai-prod" runs the same flow for each target in sequence.

### Four layers — what each tests

**Layer 0 — mint a fresh analyst PAT (OPTIONAL pre-step, programmatic only)**

What it exercises (server-side, before any sub-Claude fires): the admin
programmatic onboarding path that a real operator runs the day they
provision a new analyst. Concretely:

```
POST /api/users                    create non-admin user
POST /api/users/<id>/set-password  give them a known password
POST /api/admin/groups             new group (e.g. "e2e-analysts-<ts>")
POST /api/admin/groups/<gid>/members   add user to group
POST /api/admin/grants             grant 1–2 tables to the group (table grants
                                   need AGNES_ENABLE_TABLE_GRANTS=1 on server)
POST /auth/token                   form-login as the new analyst → session JWT
POST /auth/tokens                  mint analyst PAT (1-day TTL default)
```

The output is a new MD file in the prompt library:
`~/.config/agnes-e2e/prompts/<target>-analyst.md` — same as the admin paste-
prompt but with the analyst's PAT substituted in. Layers 1–3 then run against
that prompt to exercise the **non-admin** code paths.

Why this matters: every other layer of the spec runs with the operator's
admin PAT. That hides an entire class of UX issues — `agnes catalog` is
RBAC-filtered, some admin-only operations silently 403, the install
paste-prompt was written assuming admin scope. The discovery run with an
admin PAT showed 16 catalog entries; the same workspace bootstrap with an
analyst PAT (granted 2 tables) showed 5 (3 internal + 2 granted). The
behaviour difference is the test signal.

PASS contract: every API call returns 2xx, the analyst PAT is non-empty,
and `agnes catalog` against the new PAT returns *fewer* tables than the
admin PAT would (RBAC actually filtered something).

Why this is "Layer 0" rather than "Layer 1.5": it does not invoke a
sub-Claude at all. Pure server-side API calls + bash. Cheap (no Anthropic
budget), fast (~5 seconds wall-clock), and a hard prerequisite for the
analyst-persona variant of layers 1–3.

Cleanup contract: layer 0 records the created user-id / group-id / token-id
in `<run-dir>/.layer-0-state` so a post-test cleanup step (or a separate
`scripts/e2e-live/cleanup-analyst.sh`) can revoke the PAT, remove the
membership, drop the grants, and deactivate the user. The audit log
keeps the trail.

**Layer 1 — install + bootstrap (paste-prompt, fresh sub-Claude session)**

What it exercises: `uv tool install`, the paste-prompt's POSIX guard, `agnes
init` (PAT auth, workspace download, materialization), the lazy-mkdir contract
on the workspace, `agnes catalog` (RBAC-filtered table list), `agnes
refresh-marketplace --bootstrap`, `agnes diagnose`.

PASS contract (rather than the obvious "every step rc=0"): the workspace
exists with the expected file inventory (`CLAUDE.md`, `AGNES_WORKSPACE.md`,
`.claude/settings.json`, `user/duckdb/analytics.duckdb`), `agnes catalog`
returned a non-zero count of tables, and `agnes diagnose` produced
parsable output. Steps that are *expected* to fail until specific items in
[#345] land (`refresh-marketplace --bootstrap` until item A) are marked
`KNOWN-FAIL` rather than `FAIL` in the report; the layer overall still
passes. When item A is fixed, the corresponding `KNOWN-FAIL` flips to a
required `PASS` and the run will start failing if it regresses — that's
the signal we use to validate the fix.

Why fresh session: paste-prompt is a one-shot bootstrap. A resumed session
already has tools loaded and would not exercise the install path.

Replaces / formalises: the manual operator-end-to-end walkthrough that
`docs/testing/vm_test_plan.md` half-documented and `docs/testing/e2e_clean_analyst_bootstrap.md`
implied but never bootstrapped.

**Layer 2 — query matrix (resumed sub-Claude, scripted prompt)**

What it exercises: `agnes schema <local>`, `agnes schema <remote>`, `agnes
query "<sql>"` (local), `agnes query --remote "<sql>"`, `agnes snapshot create
--estimate`, `agnes snapshot create` (small fetch), `agnes snapshot drop`.

Why resumed: `agnes init` already happened in layer 1 — we reuse that
workspace and the same session-id. Keeps the artefact set coherent (one
session, three turns visible in `/resume` picker on the operator's machine).

Subsumes: slices 3-8 of `docs/testing/e2e_clean_analyst_bootstrap.md` (the
read-only smoke matrix slices), and adds the write-paths (snapshot create
+ drop) the legacy doc punted on.

**Layer 3 — analyst-style E2E (resumed sub-Claude, open prompt)**

What it exercises: the sub-Claude is told "invent a realistic business
question from the catalog and answer it end-to-end" — it picks the question,
chooses local vs remote vs JOIN-via-snapshot, decides when to `--estimate`,
and produces a markdown answer table. Plus a structured "Friction
encountered" section the prompt asks for explicitly.

Why this layer matters: it is the only test that catches catalog-UX issues
(items E-I on issue #345 came out of this layer). The first two layers
exercise commands; this one exercises *deciding what to do next*, which is
how real analysts actually use the system.

This is the layer with the loosest pass/fail contract — the sub-Claude
might pick a question that requires a snapshot, or one that an aggregate
answers. We score it on whether the final answer renders, whether the
CLI surface used was clean (no Traceback, no rc=0-on-fail), and whether
the Friction section is non-empty (a signal that the sub-Claude found
something to comment on, which is the failure mode we *want* to surface).

### Shared bootstrap (`_common.sh`)

```
e2e_init_run() {
  TS=$(date -u +%Y%m%dT%H%M%SZ)
  RUN_DIR="/tmp/agnes-e2e/run-$TS"
  mkdir -p "$RUN_DIR"
  uuidgen | tr '[:upper:]' '[:lower:]' > "$RUN_DIR/.session-id"
  echo "$RUN_DIR"
}

e2e_record() {
  local run_dir="$1" cast_name="$2" cmd="$3"
  asciinema rec --overwrite "$run_dir/$cast_name.cast" \
    --command "$cmd" --idle-time-limit 3 --cols 200 --rows 50
}

e2e_render_video() {
  local cast="$1"
  agg "$cast" "${cast%.cast}.gif" --speed 2
  ffmpeg -y -i "${cast%.cast}.gif" -movflags +faststart -pix_fmt yuv420p \
    -vf "pad=ceil(iw/2)*2:ceil(ih/2)*2" "${cast%.cast}.mp4"
}

e2e_sub_claude() {
  local prompt_file="$1" session_id="$2" sys_prompt="$3" max_budget="${4:-2}"
  claude --print \
    --resume "$session_id" \
    --permission-mode bypassPermissions \
    --max-budget-usd "$max_budget" \
    --append-system-prompt "$sys_prompt" \
    --verbose \
    "$(cat "$prompt_file")"
}
```

Each layer composes from these primitives + its own prompt file. No layer
re-implements workspace creation, session-id minting, or asciinema wrapping.

### Auth — paste-prompts as a per-target prompt library

The operator maintains a small library of paste-prompts as markdown files,
one per target instance, under `~/.config/agnes-e2e/prompts/` (mode 700,
gitignored, outside any repo):

```
~/.config/agnes-e2e/prompts/
├── foundryai-dev.md      ← paste-prompt from agnes-development.groupondev.com
├── foundryai-prod.md
├── agnes-dev.md          ← paste-prompt from the Keboola dev VM
├── agnes-prod.md
└── localhost.md          ← paste-prompt from a local uvicorn dev instance
```

Each `.md` is a verbatim paste-prompt that the operator copied from the
target's `/setup?role=analyst` "Generate prompt" panel — URL, PAT, and the
full step-by-step body. The file is the single source of truth for "this
PAT, this server, this install instructions" against one target.

The runner reads them by name:

```bash
scripts/e2e-live/run.sh --prompt foundryai-dev      # uses ~/.config/agnes-e2e/prompts/foundryai-dev.md
scripts/e2e-live/run.sh --prompt agnes-dev
```

Why this model wins over the "one PAT in an env var":

- **The paste-prompt is the contract.** Server change → paste-prompt change
  → MD file change. The runner doesn't need its own knowledge of install
  steps; it ships what the server says to ship. This means the same runner
  works against agnes-prod and a freshly-deployed dev instance without
  conditional logic.
- **Targets are addable in one step.** Operator runs the install panel
  against a new deployment, pastes the result into a new MD, runs
  `run.sh --prompt <name>`. No code change, no config schema migration.
- **Refresh has one motion.** PAT expires → mint a new one on the same
  instance → overwrite the MD. The runner picks it up automatically.
- **Discoverable** — `ls ~/.config/agnes-e2e/prompts/` enumerates every
  target the operator has set up.

Why not automate the mint:

- `/setup?role=analyst` returns the HTML template but the PAT is injected
  by JS after a logged-in user clicks "Generate prompt" — no PAT in the
  raw HTML.
- Playwright + storage state would work but adds a dependency layer the
  layers above don't otherwise need. Reserved for a future "layer 0 — UI
  smoke" if we want it.
- A direct `POST /api/auth/tokens` would need a long-lived admin PAT to
  bootstrap, same chicken-and-egg.

The MD-library path is the simplest stable contract that also tests the
*paste-prompt itself* — if the install panel produces a broken prompt
(missing step, wrong URL, malformed PAT), we surface that.

### Cross-target validation (a side benefit, validated empirically)

Running the same `run.sh --prompt <X>` against multiple targets is itself a
test: if `--prompt foundryai-dev` and `--prompt agnes-prod` produce the same
shape of layer-1 PASS/FAIL but `--prompt foundryai-prod` fails an assertion,
either the prod paste-prompt has drifted or there's a real prod-only
regression. The diff between two report.md files is a tight diff.

The discovery run did exactly this swap (dev → prod, same machine, same
runner, only the paste-prompt MD file changed) and it surfaced two
genuine cross-target findings inside ~$0.75 of additional budget:

1. **Item A (marketplace URL hardcode) is not a dev-only artefact** — both
   targets failed at step 6 with the same DNS-unresolvable on the same
   hardcoded host. Confirmed it's a CLI-side regression, not a deployment
   misconfiguration on one VM.
2. **Paste-prompt format drift surfaced** — the dev paste-prompt and the
   prod paste-prompt differ in step 2 (silent `mkdir` vs an interactive
   `pwd` STOP gate that headless callers can't answer). Two valid install
   prompts in isolation, but a real inconsistency the cross-target sweep
   makes visible.

Cost-vs-signal: ~$0.75 + 3 minutes wall-clock for the second run. If we
adopt this as a CI artifact, every N-target sweep adds ~$0.75 × (N-1) to
the run cost. The diff between report.md's is the regression signal.

### Inputs

```
~/.config/agnes-e2e/prompts/<target>.md    REQUIRED. The verbatim paste-prompt;
                                            URL + PAT + install instructions
                                            are all inside it. No separate
                                            SERVER_URL or PAT env var.
~/.config/agnes-e2e/<target>.env           OPTIONAL. Per-target overrides if
                                            the operator wants to tune:
  MAX_BUDGET_USD          per-layer Anthropic budget cap
                          (default: 2 for dev targets, 0.5 for prod)
  LOCAL_TABLE_HINT        starting point for layer 2 (e.g. "order_economics")
  REMOTE_TABLE_HINT       starting point for layer 2 (e.g. "<some-remote-table>")
  ANALYST_QUESTION_SEED   layer-3 starting question. If unset (default),
                          sub-Claude invents from the catalog. If set, the
                          seed is appended to the layer-3 prompt as "start
                          from this question: <seed>". Useful only when
                          comparing two runs side by side; the default open
                          variant is what catches the catalog-UX class of
                          issues.
```

The MD file is the only required input. `*_HINT` and budget overrides are
nice-to-haves the runner falls back on sensible defaults for.

`*_HINT` are not enforced — layer 2 / 3 use them as starting points but the
sub-Claude is free to pick others from `agnes catalog`. The hints make the
test deterministic enough to compare across runs while staying robust to
catalog churn.

The runner derives `SERVER_URL` (for sanity-check reachability before
firing sub-Claude) and a sanitised target label (for artefact dir naming
and `report.md` headers) by parsing the MD file's first paragraph for
`Server: https://…` and the file's basename.

### Outputs (per run)

```
/tmp/agnes-e2e/run-<ts>/
├── .session-id                       sub-Claude session uuid
├── prompt.txt                        paste-prompt (layer 1) or layer prompts
├── run-layer-{1,2,3}.sh              the runner that fired
├── session-{install,query,analyst}.{cast,gif,mp4}    asciinema artefacts
├── FoundryAI/                        the workspace sub-Claude built
└── report.md                         PARSED PASS/FAIL summary

~/.claude/projects/-private-tmp-agnes-e2e-run-<ts>/<uuid>.jsonl
                                      full sub-Claude transcript (~hundreds of
                                      events; the actual source-of-truth for
                                      what happened in each layer)
```

`report.md` is generated/appended by `_parse-transcript.py` *after each
layer* (not just at the end) — so a layer-1 fail still leaves a usable
report. `run.sh` invokes the parser as part of each layer's exit handler
regardless of layer rc, then aggregates the per-layer sections into the
final report. The parser reads:

- the per-layer `session-*.cast` (asciinema metadata, e.g. wall-clock time)
- the shared JSONL transcript at `~/.claude/projects/.../<uuid>.jsonl`,
  scoped to events from each layer's `--resume` boundary onwards

It emits:

- per-layer PASS/FAIL with the asserted predicates (workspace files exist,
  no "Traceback" in any tool_result, expected tool_use shape per layer)
- bash call inventory
- final sub-Claude text per layer
- total turns + total budget spent
- any "Friction encountered" bullets from layer 3

### How to run

```bash
# one-time setup per laptop
$ brew install asciinema agg ffmpeg
$ mkdir -p ~/.config/agnes-e2e/prompts && chmod 700 ~/.config/agnes-e2e/prompts

# add a target (repeat for each instance you want to test against)
$ cat > ~/.config/agnes-e2e/prompts/foundryai-dev.md <<'EOF'
<paste the entire prompt panel content from
 https://agnes-development.groupondev.com/setup?role=analyst — server URL,
 PAT, instructions, all of it>
EOF
$ chmod 600 ~/.config/agnes-e2e/prompts/foundryai-dev.md

# every-run — invoke the agent from the main Claude Code session
```

In Claude Code (main session):

```
Agent(subagent_type: "agnes-e2e-tester",
      prompt: "test foundryai-dev")
```

That's it. The agent does the rest:

- finds `~/.config/agnes-e2e/prompts/foundryai-dev.md` (admin)
- checks for `foundryai-dev-analyst.md`; if missing, runs Layer 0 to mint
  a fresh analyst PAT and writes the file
- runs layers 1+2+3 against the admin paste-prompt
- runs layer 1 against the analyst paste-prompt (RBAC-filtered persona)
- calls `_cleanup-analyst.sh` to tear down the Layer-0 user
- returns a markdown report to the main Claude session

The caller can scope what runs:

```
Agent(prompt: "test foundryai-dev — layers 1 only")          # quick smoke
Agent(prompt: "test foundryai-dev — admin only, layers 1+2") # skip analyst
Agent(prompt: "sweep foundryai-dev and foundryai-prod")      # cross-target
Agent(prompt: "test foundryai-dev — keep workspace")         # don't rm /tmp/...
```

Other intents the agent recognises from natural language in the prompt:

- "keep workspace" / "don't clean up" → preserve `/tmp/agnes-e2e/run-<ts>`
  on success (it's already preserved on FAIL anyway).
- "reuse <ts>" / "resume <ts>" → skip layer 1 and resume into an existing
  run dir's session. Useful for developer iteration on layer-2/3 prompts
  without paying layer 1 again.
- "skip video" / "no video" → skip the agg+ffmpeg render (cast only).
- "no cleanup" → skip the Layer-0 user teardown after the analyst run.
- "just layer 0" → mint a fresh analyst paste-prompt, then stop (useful
  when refreshing `<target>-analyst.md` ahead of a manual walkthrough).

## Cost & guardrails

Single-run budget against dev: **~$1 of Anthropic budget per full 3-layer
walkthrough** (observed on the discovery run: $0.97 for the full sequence).
Set `MAX_BUDGET_USD=2` per layer as the safety cap.

BigQuery scan cost: layer 2 and 3 narrow remote queries with WHERE / LIMIT
and prefer `agnes snapshot create --estimate` over full fetches. The
discovery run trippied no cost guardrail and stayed under $0.001 of BQ
scan cost across both query layers. We do NOT need a separate BQ budget
cap; the existing server-side `bigquery.max_bytes_per_materialize`
guardrail handles it.

## Failure handling

Three failure modes, each with a deterministic response:

1. **Layer 1 fail (install / bootstrap broken).** Layers 2 and 3 cannot
   start without a working workspace. `run.sh` exits with the layer-1
   exit code; the artefact directory is preserved; the operator opens
   `session-install.cast` and the JSONL transcript to debug.
2. **Layer 2 fail (query path broken).** Layer 3 still runs — it might
   pick a different surface and still produce an answer. Report flags
   layer 2 as FAIL but the run continues.
3. **Sub-Claude budget exceeded mid-layer.** `--max-budget-usd` aborts
   the sub-Claude; the layer fails; the report records the budget figure
   for tuning.

Operators don't fix in place — for each failure the run is preserved and
either filed against the umbrella E2E issue or fixed in a focused PR.

## Open questions

1. **Where does this live in CI?** Three options:
   (a) GitHub Actions on PR (cost: ~$1 of Anthropic per PR, plus runner minutes;
       blocks PRs that surface real regressions, but every flaky-AI moment
       blocks too);
   (b) GitHub Actions nightly against dev (cheap signal, doesn't block);
   (c) Operator runs manually pre-release (zero CI cost, depends on
       discipline). The discovery favoured (c) for now — the first few
       runs will surface enough to iterate on stability before automation
       earns its keep.
2. **Does layer 3 deserve a deterministic "scored" variant?** The current
   design lets sub-Claude invent the question, which is great for catching
   catalog-UX issues but bad for "is the answer correct?" There may be a
   future layer-3-deterministic that asks a fixed question with a known
   answer, used for regression scoring.
3. **Plugin/marketplace flow.** Layer 1 currently expects
   `refresh-marketplace --bootstrap` to fail until [#345 item A] is fixed.
   Once it is fixed, layer 1 needs to be extended to assert the marketplace
   clone, the plugin list, and a smoke `claude plugin list` against a
   second sub-Claude session that picks up the new plugins.
4. **UI smoke as "layer 0."** The original brainstorm proposed a Playwright
   walk over 5-7 admin/analyst pages with screenshot + assert 200/no-5xx.
   We dropped it for this round because the operator pastes the PAT (UI
   not needed). Worth picking up if we ever want one button to run the
   *whole* surface, not just the CLI / analyst loop.

## Non-goals

- This is not a replacement for unit tests, integration tests, or webapp
  route tests. It is a live integration smoke that complements them.
- Not a full UI test. We are not testing every admin page; that's separate
  work.
- Not a load test. Single sub-Claude session, one query at a time.
- Not a substitute for the manual "operator opens browser and tries it"
  spot check before a public-facing release. The recorded videos help
  document the run, but they don't replace human eyeballs.

## What we learned during discovery (informs this design)

The three-layer split was not in the original brainstorm — the user
walked it incrementally during the discovery session. Layer 1 came out
of "give me the install paste-prompt and watch what sub-Claude does,"
layer 2 out of "but you didn't actually query — test remote query," and
layer 3 out of "let's see what a real analyst run looks like, not just
test invocations." Each layer surfaced a distinct category of issue:

- **Layer 1** surfaced infrastructure issues (`marketplace.git` URL hardcoded,
  shell glob no-match on `?`, `diagnose` UX).
- **Layer 2** surfaced CLI-shape issues (`rc=0` on HTTP failure, `--json` alias
  missing, server-side regression on schema/snapshot endpoints — fixed before
  layer 3 ran).
- **Layer 3** surfaced data-model / catalog-UX issues (no shared join key,
  no `--estimate` on `query --remote`, schema header conflates source/engine,
  no documented entity linkage, `where_examples` unevenly populated).

That split — *infrastructure → CLI shape → data UX* — is how the layers'
boundaries are drawn in the design. The cost of running just layer 1
(~$0.3) vs running all three (~$1) is enough difference that PR-level
runs probably stick to layer 1 + 2; analyst-flow regressions get caught
nightly with the full three.

### Layer 0 (analyst persona) — what the third walkthrough surfaced

After the dev (admin) and prod (admin) runs, a programmatic "Layer 0 →
Layer 1 against the analyst PAT" walkthrough on dev added three findings
that no admin run could have surfaced:

- **Catalog 5 vs 16.** The analyst PAT (granted `order_economics` +
  `s1_session_landings`) saw 5 catalog entries (3 internal + 2 granted)
  vs the admin's 16. RBAC filter behaves correctly — and the "fewer
  tables for an analyst" signal is itself the layer-0 PASS contract.
- **RBAC filtering is silent.** No 403 / "not authorized" surfaced
  anywhere during the analyst run. The analyst sees a smaller catalog,
  *not* an error message. For a fresh analyst this means: "I have no
  signal that other tables exist and I just lack grants." Worth a
  follow-up — either a `--include-ungranted` flag with a "you can ask
  admin for grant on these tables" view, or the analyst-facing docs
  spell it out.
- **`agnes diagnose` reports the raw row count, ignoring RBAC.** Output
  showed `data: 11 tables` (= the registry row count) while `agnes
  catalog` showed 5 (RBAC-filtered). Analysts will notice the mismatch
  ("I see 11 in diagnose, 5 in catalog, why?") and ask why. Diagnose
  should probably scope to the caller's role.

Plus a fourth finding that's specific to the prompt library design:

- **Wheel-version drift in the stored paste-prompt.** The original dev
  paste-prompt MD pinned `agnes_the_ai_analyst-0.54.28-py3-none-any.whl`;
  by the time the analyst walkthrough ran, the server had rolled to
  0.54.29 and the 0.54.28 wheel URL returned 404. A fresh-machine run
  would have failed at step 1. The MD library design therefore needs a
  refresh discipline (or a wheel-version unpinning at the paste-prompt
  generation side — `/cli/wheel/latest` redirect). See the issue
  comment for the full write-up.

Cost summary for the three layer-0+layer-1 admin/prod/analyst runs:
~$2.80 of Anthropic budget, ~9 minutes wall-clock total, 11 distinct
issues filed against issue #345 (4 CLI + 5 catalog-UX + 2 cross-target
+ wheel-version-drift, RBAC-silent, diagnose-scope).
