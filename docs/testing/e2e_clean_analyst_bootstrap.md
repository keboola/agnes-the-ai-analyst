# E2E Verification: clean-analyst-bootstrap (PR #173)

End-to-end verification of the clean-analyst-bootstrap rewrite on a deployed
VM. Designed for parallel sub-agent dispatch — Phase 0 prerequisites run
sequentially, then 10 parallel slices in a single Claude Code message, then
sequential hook test, then aggregation.

**Estimated runtime:** ~15-20 min total (5 min Phase 0 + 5-10 min Phase 1
parallel + 3 min Phase 2 + 1 min aggregation).

**How to use:** open Claude Code on the VM in any cwd; paste this entire
document into the conversation. Claude Code will execute Phase 0, then
dispatch the 10 slices in parallel via `Agent` tool calls in a single
message, then guide you through Phase 2.

## Prerequisites — fill these in before starting

- **Server URL:** `https://<your-agnes-host>`
- **Active user:** confirm via web login (Google OAuth or password works)
- **Test PAT:** mint via web `/setup?role=analyst` → click Generate prompt → copy clipboard → extract the PAT. Save as `$TEST_PAT`.
- **Picked test tables:**
  - `LOCAL_TABLE`: any `query_mode='local'` table from `agnes catalog --json`
  - `REMOTE_TABLE`: any `query_mode='remote'` BigQuery table (preferably small or partitioned)
  - `REMOTE_WHERE`: a simple WHERE clause that filters `REMOTE_TABLE` to a small subset (e.g., `event_date = DATE '2026-01-01'`)
  - `REMOTE_SELECT`: 1-2 columns to select from `REMOTE_TABLE`

---

## Phase 0 — Prerequisites (sequential, ~5 min)

```bash
# 1. Confirm server reachable + on the new build
curl -fsS "$SERVER_URL/api/health" | head -3
curl -fsS "$SERVER_URL/cli/version" 2>/dev/null || true   # should mention 0.32.0+ post-rename

# 2. Confirm /setup?role=analyst renders the role tiles
curl -fsS "$SERVER_URL/setup?role=analyst" | grep -E "role-tiles|Analyst workspace|const ROLE" | head -5
# Expect: at least 3 matches (CSS class, tile heading, JS const)

# 3. Mint analyst PAT (see Prerequisites above) and export it
export SERVER_URL="https://<your-agnes-host>"
export TEST_PAT="agnes_pat_..."

# 4. Bootstrap the BASE workspace that read-only slices share
mkdir -p /tmp/agnes-e2e-base
cd /tmp/agnes-e2e-base
# Paste the install prompt verbatim from /setup?role=analyst.
# After it finishes:
tree -aL 2 /tmp/agnes-e2e-base | head -30
```

**Gate:** if the base workspace doesn't have the expected shape (`CLAUDE.md`,
`AGNES_WORKSPACE.md`, `.claude/settings.json`, `user/duckdb/analytics.duckdb`),
STOP and report the failure mode — the rest of the plan assumes a working
base workspace.

---

## Phase 1 — Parallel slices (dispatch ALL 10 in ONE message, ~5-10 min)

For each slice, dispatch a `general-purpose` Agent. **Send all 10 Agent tool
calls in a single response message** so they run concurrently. Pass
`SERVER_URL` and `TEST_PAT` from Phase 0 into each prompt.

### Slice 1 — Web UI + role tiles + paste-prompt content

```text
Verify the /setup?role=analyst and /setup?role=admin pages on $SERVER_URL.

Required checks:
- /setup?role=analyst renders 2 role tiles (Analyst workspace + Admin CLI);
  Analyst tile has is-active class, Admin tile is inactive.
- Page contains `const ROLE = "analyst"` (JSON-escaped form, with quotes).
- The JS array `SETUP_INSTRUCTIONS_TEMPLATE` contains `agnes init` and
  `agnes catalog`, does NOT contain `claude plugin marketplace add`,
  `agnes auth import-token`, or `agnes diagnose`.
- /setup?role=admin: admin tile active, prompt contains `agnes auth import-token`.
- /install (no query) returns 302 to /setup.

Tools: curl + grep/python regex.
Report: PASS/FAIL per check + 5 lines of the rendered prompt for both roles.
```

### Slice 2 — `agnes init` workspace inventory

```text
In /tmp/agnes-e2e-init (NEW empty folder):
  agnes init --server-url $SERVER_URL --token $TEST_PAT --workspace .

Verify EXACTLY this file set is present:
- CLAUDE.md (non-empty, contains "agnes pull")
- AGNES_WORKSPACE.md (does NOT contain $TEST_PAT, no "{placeholder}" leaks,
  contains $SERVER_URL, contains the absolute workspace path,
  has 6 H2 sections matching ^## )
- .claude/settings.json (hooks.SessionStart with `agnes pull --quiet`,
  hooks.SessionEnd with `agnes push --quiet`, has `model` and `permissions.allow`)
- .claude/CLAUDE.local.md (stub content "# My Notes")
- user/duckdb/analytics.duckdb (file exists, non-zero size)

Verify NONE of these exist:
  data/parquet, data/duckdb, data/metadata, user/artifacts, .agnes

Conditional dirs (per lazy-mkdir contract):
- if server/parquet/ exists, must contain ≥1 .parquet
- if .claude/rules/ exists, must contain ≥1 km_*.md

Run: tree -aL 3 /tmp/agnes-e2e-init
Report: full file inventory + PASS/FAIL per assertion.
```

### Slice 3 — Reader smoke matrix (read-only against base workspace)

```text
cd /tmp/agnes-e2e-base
For each command, capture exit code + first stderr line.
Forbidden: any "Traceback" in stderr.

  agnes catalog
  agnes catalog --json
  agnes catalog --metrics
  agnes schema $LOCAL_TABLE
  agnes describe $LOCAL_TABLE -n 5
  agnes status
  agnes status --json
  agnes diagnose
  agnes diagnose system
  agnes disk-info
  agnes auth whoami
  agnes skills list
  agnes skills show agnes-data-querying

Bad-input no-crash:
  agnes schema __nonexistent__
  agnes describe __nonexistent__
  agnes explore __nonexistent__

PASS = all rc ∈ {0, 1}, no Traceback.
Report: command-by-command rc + traceback-Y/N + first stderr line.
```

### Slice 4 — Local + remote query paths

```text
cd /tmp/agnes-e2e-base

Local (parquet/DuckDB) path:
  agnes query "SELECT count(*) FROM $LOCAL_TABLE LIMIT 1"
  agnes query "SELECT * FROM $LOCAL_TABLE LIMIT 3"
  agnes query --json "SELECT count(*) FROM $LOCAL_TABLE"
  agnes explore $LOCAL_TABLE   # friendly even in non-TTY

Remote (BigQuery passthrough) path:
  agnes query --remote "SELECT count(*) FROM $REMOTE_TABLE LIMIT 1"
  agnes query --remote "SELECT count(*) FROM $REMOTE_TABLE" --limit 1

Report: per-query rc, first 5 lines of stdout, traceback-Y/N.
Flag any 502 / cost-guardrail / typed-error envelopes.
```

### Slice 5 — Snapshot lifecycle

```text
cd /tmp/agnes-e2e-base

  agnes snapshot list                                            # baseline
  agnes snapshot create $REMOTE_TABLE --select $REMOTE_SELECT --where '$REMOTE_WHERE' --as e2e_test_snap --estimate
  agnes snapshot create $REMOTE_TABLE --select $REMOTE_SELECT --where '$REMOTE_WHERE' --as e2e_test_snap
  agnes snapshot list                                            # should show e2e_test_snap
  agnes query "SELECT count(*) FROM e2e_test_snap"
  agnes snapshot refresh e2e_test_snap
  agnes snapshot drop e2e_test_snap
  agnes snapshot list                                            # back to baseline

PASS = clean lifecycle, snapshot file present after create + absent after drop.
Report: per-step rc + first stdout line.
```

### Slice 6 — Force / protection scenarios

```text
Setup:
  cp -r /tmp/agnes-e2e-base /tmp/agnes-e2e-force
  cd /tmp/agnes-e2e-force
  echo "# my private edit" > .claude/CLAUDE.local.md

Test 1: re-init without --force should refuse
  agnes init --server-url $SERVER_URL --token $TEST_PAT --workspace .
  Expect rc=1, stderr contains "already initialized" or "partial_state",
  no Traceback.

Test 2: --force regenerates CLAUDE.md but PRESERVES CLAUDE.local.md
  agnes init --server-url $SERVER_URL --token $TEST_PAT --workspace . --force
  Expect rc=0
  Then: cat .claude/CLAUDE.local.md  → must still contain "# my private edit"

Report: PASS/FAIL per test + grep result for the private edit marker.
```

### Slice 7 — Pre-init reader smoke (no-traceback contract)

```text
mkdir -p /tmp/agnes-e2e-pre && cd /tmp/agnes-e2e-pre

For each, capture rc + stderr:
  agnes query "SELECT 1"
  agnes snapshot create __nope__ --as x --estimate
  agnes explore foo
  agnes snapshot list
  agnes status
  agnes catalog
  agnes disk-info

Forbidden: any "Traceback" in stderr.
Expected: rc=1 with friendly hint mentioning "agnes init" or "agnes pull".

Report: per-command rc + traceback-Y/N + did-hint-mention-init-or-pull.
```

### Slice 8 — Auth + token lifecycle

```text
agnes auth whoami     # should print email + role

# Token CRUD
agnes auth token list
TID=$(agnes auth token create e2e-test --expires-in-days 1 | grep -oE 'tok_[a-zA-Z0-9_-]+' | head -1)
agnes auth token list   # new token visible
agnes auth token revoke "$TID"
agnes auth token list   # revoked_at populated for $TID

# Bad token → friendly 401
AGNES_TOKEN=fake-pat agnes catalog 2>&1 | tail -5
# Expect friendly hint, no Traceback

Report: PASS/FAIL per step + the captured TID.
```

### Slice 9 — Admin metrics + catalog --metrics

```text
# Read paths (any analyst)
agnes catalog --metrics
agnes catalog --metrics --show revenue/mrr   # adjust to a real metric path

# Write paths (admin only — skip if the test user isn't admin)
agnes admin metrics --help    # surface check (import/export/validate listed)

If admin:
  agnes admin metrics validate
  agnes admin metrics export /tmp/metrics-backup
  ls /tmp/metrics-backup/ | head

Report: per-command rc + first stdout line.
```

### Slice 10 — AGNES_WORKSPACE.md content quality

```text
cd /tmp/agnes-e2e-base
cat AGNES_WORKSPACE.md | head -100

Programmatic checks:
- Contains "Created:" header line with ISO timestamp
- Contains "Server:" line with $SERVER_URL
- Contains "Workspace:" line with absolute path
- $TEST_PAT does NOT appear anywhere in the file
- No literal "{created_at}", "{server_url}", "{workspace_path}" substrings
- Has exactly 6 H2 sections (^## )
- Cheat sheet section mentions: agnes catalog, agnes query, agnes pull,
  agnes snapshot create, agnes status
- Uninstall section mentions: uv tool uninstall, ~/.config/agnes, ~/.agnes
- For each path in the "Globally installed" table that's a real file path
  (~/.local/bin/agnes, ~/.config/agnes/{config.yaml,token.json}):
  test -e and report exists/missing.

Report: each assertion PASS/FAIL.
```

---

## Phase 2 — Hook behavior (sequential, ~3 min)

Sub-agents can't open Claude Code sessions, so this part is manual.

```bash
cd /tmp/agnes-e2e-base
claude                    # opens Claude Code; SessionStart hook fires `agnes pull --quiet`

# Inside Claude Code, ask: "show me 5 rows of $LOCAL_TABLE"  → should work without errors

/exit                     # SessionEnd hook fires `agnes push --quiet`

ls /tmp/agnes-e2e-base/user/sessions/    # should be non-empty (transcript captured)

# Re-enter to verify SessionStart fires again
claude
/exit
```

**Verify in server audit log:** 2× `agnes pull` GETs (one per session start)
+ 2× `agnes push` POSTs (one per session end). Tail the server audit endpoint
or DB table to confirm.

---

## Phase 3 — Aggregation

Compile a single PASS/FAIL table:

| Slice | Status | Notes |
|---|---|---|
| 1 — Web UI role tiles | … | … |
| 2 — agnes init inventory | … | … |
| 3 — Reader smoke matrix | … | … |
| 4 — Query paths | … | … |
| 5 — Snapshot lifecycle | … | … |
| 6 — Force / protection | … | … |
| 7 — Pre-init no-traceback | … | … |
| 8 — Auth + tokens | … | … |
| 9 — Admin metrics | … | … |
| 10 — AGNES_WORKSPACE.md | … | … |
| Hooks (Phase 2) | … | … |

For any FAIL: preserve the failing folder/output, report the exact command
+ first traceback / first stderr line. Don't fix in place — flag for follow-up.

---

## Cleanup after testing

```bash
rm -rf /tmp/agnes-e2e-base /tmp/agnes-e2e-init /tmp/agnes-e2e-force /tmp/agnes-e2e-pre /tmp/metrics-backup
agnes auth token revoke <e2e-test PAT id>
```

---

## Slice priority

If time is constrained, run these load-bearing slices first:

1. **Slice 1** (Web UI role tiles) — proves the web entry point works.
2. **Slice 2** (agnes init inventory) — proves the bootstrap creates the
   exact expected file set with no dead dirs.
3. **Slice 7** (pre-init no-traceback) — proves the reader contract holds.
4. **Slice 10** (AGNES_WORKSPACE.md content) — proves the human-facing
   docs render correctly with no PAT leak.

Slices 3-6, 8-9 are breadth coverage — important but lower-priority.

---

## Coverage honesty — what this plan reveals (and what it doesn't)

**This plan ≠ exhaustive.** It catches contract-level bugs — file inventory,
no-traceback reader contract, JS ternary direction, lazy-mkdir compliance.
It does NOT catch a meaningful slice of real-world failure modes.

### What the plan reveals (✅)

- **Workspace inventory bug** — missing/extra file after `agnes init`
- **No-Python-traceback contract** for every reader command
- **PAT leak** into `AGNES_WORKSPACE.md`
- **JS ternary direction bug** — analyst tile mints admin PAT
- **Lazy-mkdir violation** — pre-allocated empty directories
- **Force vs no-force** — regenerates CLAUDE.md but preserves CLAUDE.local.md
- **Snapshot lifecycle** — estimate → fetch → query → drop
- **Auth flow** — token CRUD, 401 friendly errors
- **Help text / flag surface** — every CLI command's `--help` lists expected flags
- **Hook command shape** — SessionStart→`agnes pull`, SessionEnd→`agnes push`

### What the plan does NOT reveal (❌)

| Gap | Why this plan misses it |
|---|---|
| **Cross-platform** (Windows / Linux / macOS) | VM is one OS. `setup_instructions.py` has 200+ lines of platform-specific TLS logic that never runs |
| **Private CA bootstrap** | If VM uses public TLS, the entire step-0 trust block (cert install, OS keychain, `~/.agnes/ca-bundle.pem`) stays unexercised |
| **Migration from old `da` workspace** | Greenfield rewrite — but existing analysts have old data. Does `agnes init` behave well in a folder where `da analyst setup` previously ran? Unknown |
| **Network failures** — slow / packet loss / mid-pull 5xx | Local tests don't mock flaky network |
| **Concurrent users** — 2 analysts paste-prompt simultaneously | Single-user happy path only |
| **Disk full during pull** | Not simulated |
| **PAT expiry mid-session** | 1 h TTL → hooks 401 after expiry. What happens to the next session? Untested |
| **Unicode / locale in workspace paths** | `--workspace /tmp/path with spaces and ěščřž/` |
| **Read-only HOME** / non-standard permissions | `agnes init` writes to `~/.config/agnes/` — what if it can't? |
| **Browser variance** — paste-prompt clipboard | Different copy semantics across Safari/Chrome/Firefox/clipboards |
| **Claude Code version drift** | Spec assumes a specific `.claude/settings.json` hook schema |
| **Admin CLAUDE.md override edge cases** | Custom template without `_INIT_MARKER` substring — how does init behave? |
| **Long-soak** — 1+ day, hooks fire 50× | Only one-shot tests |
| **Real BQ data scale** — TB-scale tables, partition cost decisions | Test PATs/tables will be small |

### Recommended additional coverage layers

**Tier 1 (before merge):**
1. Run this E2E plan on the VM — ~15-20 min, catches ~70 % of typical bugs.
2. **Manual smoke with the actual analyst use-case** — the person who'll
   use this opens a fresh workspace, asks 5 real questions, watches what
   breaks. Best source of surprises.

**Tier 2 (before broad rollout):**
3. **Soak test** — one analyst uses it for a week. Hooks fire ~10×/day.
   Anything that accumulates (sessions, snapshots, log files) surfaces.
4. **Migration test** — find an existing `da` user, walk them through
   `uv tool uninstall agnes-the-ai-analyst` → reinstall → `agnes auth
   import-token` → `agnes init --force` in their existing folder. Watch
   for confusion / breakage.

**Tier 3 (nice-to-have):**
5. Cross-platform — if you have Windows/Linux users, repeat Phase 0+1 there.
6. Private CA — if you deploy with a private CA, run a separate VM with
   that configuration and exercise the full trust-bootstrap step.
7. Network chaos — toxiproxy / `tc` to introduce latency/loss between
   client and server, verify hooks don't hang sessions.

### Realistic coverage estimate

| Layer | Coverage of analyst-visible bugs |
|---|---|
| This plan alone | ~70 % |
| Plan + Tier 1 manual smoke | ~80 % |
| Plan + Tier 1 + Tier 2 soak/migration | ~95 % |
| Plan + all Tiers | ~98 % |

The remaining 2-5 % surfaces only in real production use across diverse
analyst workflows. Rule of thumb: ship after Tier 1 if the surface area
is small (< 10 analysts), wait for Tier 2 before scaling to a wider audience.

### Prerequisites — what you need on the VM to run this plan

| Item | Why | Falls back to |
|---|---|---|
| Server on the new build | All slices need the new `/setup?role=` endpoint + `/api/welcome` + PAT scope/TTL fields | Auto-upgrade cron picks up `:dev`/`:keboola-deploy-latest` within ~5 min of the merge tag landing |
| Web access (browser) | Phase 0 step 3 — mint a PAT via `/setup?role=analyst` "Generate prompt" button | Or manual `curl -u admin:pw -X POST /auth/tokens` if you can't open a browser |
| Account with grants | `test_pat` needs `resource_grants` for ≥ 1 `query_mode='local'` table + ≥ 1 `query_mode='remote'` BigQuery table (Slices 4 + 5). Admin role for Slice 9 | Slice 4/5/9 skip cleanly if grants missing — other slices still cover the bootstrap path |
| BigQuery configured server-side | Slice 4 (`agnes query --remote`) and Slice 5 (snapshot create) hit BQ via the server | Slices skip with a 400/501 error if BQ isn't configured — won't false-fail the plan |
| `agnes` on `$PATH` after Phase 0 step 4 | Sub-agent slices invoke `agnes` directly | If install fails, paste-prompt step itself catches it before any slices run |
| Network from VM to server | Slices use `curl` and `agnes` (which uses `httpx`) | If network is broken, Phase 0 step 1 detects it and stops |

### How to dispatch the slices in parallel

If you're running this from Claude Code on the VM:

1. Paste this entire document into the Claude Code conversation.
2. Claude Code will execute Phase 0 sequentially (no choice — each step
   gates the next).
3. After Phase 0 completes, Claude Code dispatches **all 10 slices in a
   single response message** (multiple `Agent` tool invocations in one
   block). They run concurrently.
4. Phase 2 (hook test) needs you in the loop — Claude Code will pause
   and walk you through the manual `claude` session opens.
5. Phase 3 — Claude Code aggregates and produces the final PASS/FAIL
   table.

If you're running it manually (no Claude Code agent dispatch), execute
the slices serially in 10 terminal tabs / tmux panes. Same content per
slice, just no automatic aggregation — you compile the table by hand.
