# Global (User-Scope) Distribution — Agnes in Every Repository — Design

**Date:** 2026-07-30
**Status:** Draft for review
**Verified against:** `0.77.30` (HEAD `779e9befd`); Claude Code user-scope
mechanics verified against the official docs and empirically against a
current Claude Code install (`plugin install`/`marketplace add` default
`--scope user`; `enabledPlugins`/`extraKnownMarketplaces` live in
`~/.claude/settings.json`; `claude mcp add --scope user`; user-level
`hooks`/`env`/`CLAUDE.md` apply in every project; `claude plugin list
--json` reports per-entry `scope: "user"|"project"` with
`projectPath: null` for user-scope installs; `claude plugin uninstall`
supports `--scope user|project|local`).

## 1. Context & goal

Agnes distribution today is **workspace-centric by design**: `agnes init`
bootstraps one analyst workspace — CLAUDE.md rails, SessionStart/End hooks,
marketplace plugins, parquets + local DuckDB — and everything works inside
that folder. That is the right default for analysts.

Engineers work differently: many repos in parallel, each with its own
`CLAUDE.md`/`AGENTS.md`, switching constantly. For them the ask is:

> "Install Agnes **once, globally** — skills and data access available by
> default in every repository I open — without installing anything into the
> N repos themselves."

Claude Code has a user-scope configuration layer that applies in every
project, and Agnes already has every building block (per-user filtered
marketplace, an OAuth-protected MCP endpoint, a CLI whose credentials and
workspace anchor live in the home directory). What is missing is:

1. one CLI gap — data commands resolve the workspace from cwd only, and
2. a first-class, idempotent way to enrol and converge the user scope.

**Goal:** an engineer runs one command after the normal `agnes init`, and
from then on every Claude Code session in every repository has (a) the
granted marketplace skills, (b) Agnes data tools (MCP + CLI), and (c) the
data-querying rails — with the whole layer kept up to date by the existing
`agnes update` convergence, and cleanly removable.

**Non-goals (v1):**

- No user-level SessionEnd hook — transcript push stays workspace-anchored
  (§8). (The user-level SessionStart *update* hook IS installed by default
  — owner decision, §7.3/§13 — with `--no-hook` as the opt-out.)
- No multi-workspace support — one anchored workspace per machine, as today.
- No skills-bundle sync client (`/api/user/skills-bundle` →
  `~/.claude/skills/`) — separate follow-up; the marketplace path already
  covers plugin-shipped skills.
- No automation of org-fleet managed settings — documented as an operator
  recipe only (§9).
- No change to what the analyst workspace flow writes into the workspace.

## 2. Current state (why "global" does not work today)

| Layer | Today | Effect in a foreign repo |
|---|---|---|
| Credentials + anchor | `~/.config/agnes/` (`token.json`, `config.yaml` with `server`, `workspace_root`) — already global | works everywhere |
| Marketplace clone + registration | `~/.agnes/marketplace` + `claude plugin marketplace add` (user-scope by default) — already global | visible everywhere |
| Plugin installs | `claude plugin install <p>@agnes --scope project` into **cwd** (`cli/commands/refresh_marketplace.py::_reconcile_with_manifest`) | skills invisible outside the workspace |
| Data commands | `AGNES_LOCAL_DIR` env → else **cwd** (`cli/commands/query.py::_run_local`, `cli/commands/pull.py`, `cli/mcp/server.py` `query_local`/`pull`, snapshot/status/disk-info/explore/statusline/mark-private) | "No local DuckDB yet" everywhere except the workspace |
| Rails | workspace `CLAUDE.md` from `/api/welcome` | agent has no protocol knowledge |
| Hooks | workspace `.claude/settings.json` only (`cli/lib/hooks.py`, intentionally) | no sync trigger elsewhere (by design) |

Two facts the design builds on:

- `agnes update` already resolves the workspace from anywhere
  (`cli/commands/update.py::_resolve_workspace`: `AGNES_LOCAL_DIR` →
  `workspace_root` config → cwd-if-initialised), and `agnes push` uploads
  transcripts **only** from the anchored workspace's session folder — never
  from foreign repos. The anchoring pattern exists; it is just not applied
  to the data-read commands.
- `agnes init` already writes one user-scope Claude Code setting
  (`cli/lib/automode.py::ensure_marketplace_trusted` →
  `~/.claude/settings.json`), so the merge-safely-into-user-settings
  discipline has a precedent to generalise.

## 3. Claude Code user-scope surface (verified)

What the design relies on, all confirmed against current docs + a live
install:

- **Plugins:** `claude plugin install <p>@<mkt> --scope user` (user is the
  CLI default) → enablement recorded in `~/.claude/settings.json`
  `enabledPlugins` (`"plugin@marketplace": true`) → skills/commands/agents
  from the plugin load in **every** project. A project/local-scope `false`
  can still disable one repo.
- **Marketplaces:** registration is user-scope by default
  (`extraKnownMarketplaces`); the Agnes flow already registers the local
  clone (`~/.agnes/marketplace`) this way.
- **MCP:** `claude mcp add --scope user …` → `~/.claude.json` `mcpServers`,
  available in every project. Stdio transport can carry `--env` pairs.
- **Memory:** `~/.claude/CLAUDE.md` is loaded in every project, before the
  project's own CLAUDE.md; per-repo files keep working unchanged.
- **Hooks:** user-level `hooks` in `~/.claude/settings.json` **merge** with
  project hooks (both run); hooks receive `$CLAUDE_PROJECT_DIR`.
- **Org fleet:** managed settings (MDM-deployed or server-managed) can force
  `extraKnownMarketplaces`, `enabledPlugins`, and managed MCP servers for
  every user; `strictKnownMarketplaces` locks the marketplace list.

### 3.1 End-to-end verified before implementation (2026-07-31)

The three load-bearing hypotheses were exercised on a live install (current
Claude Code, agnes CLI `0.77.27`), with full state restore afterwards:

- **Plugins:** a plugin from the Agnes-served marketplace installed via
  `claude plugin install <p>@agnes --scope user` surfaced its skill
  (`<plugin>:<skill>`) inside a headless session started in an unrelated
  directory. `claude plugin uninstall … --scope user` restored the prior
  state exactly — including a pre-existing project-scope install of the
  same plugin, whose reported `enabled` state had reflected the *merged*
  user-scope enablement during the test and reverted with it (empirical
  backing for §6.2's revert semantics).
- **Rails:** a marker-fenced block in `~/.claude/CLAUDE.md` (the §6.1
  step-4 format) was loaded verbatim by the same foreign-directory session
  (nonce echo test).
- **MCP:** `claude mcp add --scope user <name> -- <abs-path-to-agnes> mcp`
  registered the stdio server in `~/.claude.json`; `claude mcp get`
  reported it *Connected* (spawn + handshake using the saved CLI
  credentials), and the foreign-directory session listed the server. Its
  tools were still connecting at the moment the short session answered —
  the stdio server has a noticeable cold start (heavy Python imports); D4's
  docs should mention that tools appear a few seconds into a fresh session.
- **Remote MCP OAuth (the D4 alternative), end-to-end:** discovery →
  RFC 7591 dynamic registration (the server requires
  `grant_types: ["authorization_code", "refresh_token"]` — registration
  with `authorization_code` alone is rejected) → browser consent screen
  (rendered with the operator's existing web session; shows client name,
  scope, signed-in identity) → PKCE S256 code exchange (Bearer token,
  `expires_in` 8 h, scope `read`) → authenticated `initialize` +
  `tools/list` returned the full RBAC-filtered tool set; the same call
  without a token is 401. Two D4-relevant caveats: (a) `claude mcp add
  --transport http` leaves the server at "Needs authentication" — the
  OAuth round is completed from `/mcp` in an interactive session, which
  the docs recipe must say explicitly; (b) the revocation endpoint
  rejects public-client revocation (`client_secret: Field required`, an
  upstream SDK request-validation layer above our provider's working
  `revoke_token`) — tokens currently live out their TTL; tracked as a
  follow-up outside this design.

## 4. Design overview

Three code deliverables and one docs deliverable:

- **D1 — anchored workspace resolution for data commands.** Extract the
  `_resolve_workspace` idea into a shared helper and adopt it across the
  data-read/pull surface, so the CLI (and the stdio MCP server) work from
  any cwd with **no env var required**.
- **D2 — `agnes global` command group** (`enable` / `disable` / `status`):
  idempotent enrolment of the user scope — user-scope plugin installs, one
  user-scope MCP server entry, a marker-fenced rails block in
  `~/.claude/CLAUDE.md`, and a `global_scope: true` flag in
  `~/.config/agnes/config.yaml`. `disable` reverts exactly what `enable`
  wrote.
- **D3 — convergence.** `agnes refresh-marketplace` gains
  `--target user|project`; `agnes update` gains a `global` step (gated on the
  config flag) so the user scope stays reconciled by the same machinery
  that keeps the workspace fresh. A user-level SessionStart hook is
  installed by default (`--no-hook` opts out; §7.3).
- **D4 — docs.** `docs/global-distribution.md`: the engineer recipe, the
  remote-MCP alternative, and the org-fleet managed-settings appendix.

## 5. D1 — anchored workspace resolution

### 5.1 Shared helper

New `cli/lib/workspace_resolve.py`:

```python
def resolve_data_workspace() -> Optional[Path]:
    """Locate the workspace for data reads/writes, from any cwd.

    Order:
      1. AGNES_LOCAL_DIR env — explicit override, always wins even when the
         target is not workspace-shaped (sandbox/runner contract, unchanged).
      2. cwd, if workspace-shaped — preserves current behaviour exactly for
         anyone standing inside a workspace.
      3. workspace_root from ~/.config/agnes/config.yaml, if workspace-shaped
         — the new global fallback.
      4. None.
    """

def is_workspace_shaped(p: Path) -> bool:
    """(p/'.claude/init-complete').exists()
       or (p/'user/duckdb/analytics.duckdb').exists()
       or (p/'server/parquet').is_dir()"""
```

Notes:

- Step 3 checks shape too: a stale anchor (workspace deleted, machine
  migrated) degrades to `None` + the existing "no local data" hints, never
  to reads against a bogus path.
- `agnes update` keeps its own order (`env → anchor → cwd-if-initialised`,
  anchor **before** cwd) — convergence must target the anchor even when run
  from inside some other initialised folder. Data reads prefer the
  workspace you are standing in. The difference is intentional and
  documented in both docstrings.
- The chat-sandbox/runner contract is unchanged: runners set
  `AGNES_LOCAL_DIR` explicitly and step 1 wins.

### 5.2 Adoption

Replace the inline `os.environ.get("AGNES_LOCAL_DIR", ".")` pattern in:
`cli/commands/query.py`, `pull.py`, `snapshot.py`, `status.py`,
`disk_info.py`, `explore.py`, `statusline.py`, `mark_private.py`,
`self_upgrade.py` (workspace probe), and `cli/mcp/server.py`
(`query_local`, `pull`).

Behaviour deltas (all additive):

- **`agnes query` (local path):** foreign cwd + anchored workspace → queries
  the anchor instead of erroring. Unshaped cwd + no anchor → the existing
  `_LocalDbMissing` hint, extended to mention `agnes init` /
  `AGNES_LOCAL_DIR`.
- **`agnes pull`:** foreign cwd → pulls into the anchor (today it would
  silently create a `server/parquet` + `user/duckdb` tree inside whatever
  repo you are standing in — a footgun this design removes). No anchor and
  unshaped cwd → typed `partial_state` error with the `agnes init` hint
  instead of scaffolding into a random folder. A new `--workspace` option
  provides the explicit escape hatch (mirrors `agnes init --workspace`).
- **stdio MCP server:** `query_local`/`pull` work when the client spawns the
  process with cwd `/` or `$HOME` (Claude Desktop and user-scope
  registrations do exactly that). This fixes the desktop case even before
  D2 exists. Both tools' docstrings are updated to describe the new
  resolution order — tool docstrings are the agent-facing UX surface
  (command-UX standard).

`agnes push` is untouched (already anchored, `workspace_root`-only by
design).

## 6. D2 — `agnes global` command group

New Typer group registered as `global` (module `cli/commands/global_scope.py`
— `global` is a Python keyword, the module name differs from the command
name). Three subcommands, all supporting `--json` (command-UX standard).

### 6.1 `agnes global enable`

Idempotent; every step is check-then-act and reported like `agnes update`
steps. Preconditions: `claude` CLI on PATH (reuse
`refresh_marketplace._claude_base_cmd`), saved credentials verified with one
`GET /api/catalog/tables` (same probe as `agnes init` step 2). A missing
`workspace_root` does not abort — every step below still applies (skills and
the server-side MCP tools work without a workspace); `enable` just notes
that the local-data tools (`query_local`, `pull`) will resolve nothing
until `agnes init` anchors a workspace.

| # | Step | Mechanism |
|---|---|---|
| 1 | Marketplace present | If `~/.agnes/marketplace` clone or the `agnes` registration is missing, run the existing `refresh-marketplace --bootstrap` path first (reuse, not reimplement). |
| 2 | Plugins → user scope | Run the manifest reconcile (§7.1) in `target="user"` mode: install/update every plugin served by the caller's filtered marketplace with `claude plugin install <p>@agnes --scope user` (the `claude` CLI's own flag is `--scope`; our wrapper flag is `--target`, §7.1). Enablement is recorded by the `claude` CLI itself; the cwd-based `enabledPlugins` writer is skipped in this mode (§7.1, §6.4). |
| 3 | MCP server entry | `claude mcp add --scope user agnes -- <abs-path-to-agnes> mcp` (stdio). Absolute binary path resolved at enable time (`shutil.which`/`sys.argv[0]` — same detection the setup bundle uses). If an `agnes` MCP entry already exists: ours (command ends in `agnes mcp`) → converge/no-op; foreign → warn + skip (never clobber), `--force` overrides. |
| 4 | Rails block | Insert/replace a marker-fenced block in `~/.claude/CLAUDE.md` (create the file if absent). Exact markers: `<!-- BEGIN agnes-global (managed by 'agnes global enable'; edits inside are overwritten) -->` and `<!-- END agnes-global -->`. Content ships as a static CLI template (`cli/templates/global_rails.md`): ~20 lines — discovery-first protocol, `query_mode` decision table pointer, `agnes skills show agnes-data-querying` for the full version. Kept deliberately short: it is loaded into **every** session in **every** repo. Everything outside the markers is preserved byte-for-byte. |
| 5 | User-level SessionStart hook | Default ON (skip with `--no-hook`): install into `~/.claude/settings.json` a SessionStart entry with the same detached shape as the workspace one (`bash -c "( nohup agnes update --quiet … & ) ; true"`), written via the marker-aware merge (§6.4, `_OUR_COMMAND_MARKERS`) so third-party entries survive and re-runs are idempotent. |
| 6 | Flag | `save_config({"global_scope": True})`. |
| 7 | Summary | Human summary + "restart Claude Code sessions to pick it up". |

Why stdio MCP (not the remote Streamable-HTTP endpoint) as the default
registration: it reuses the already-saved PAT from `~/.config/agnes/`
(zero extra auth friction, works headless), and it carries the local-data
tools (`query_local`, `pull`) that the remote server cannot have. The
remote endpoint (`/api/mcp/http`, OAuth 2.1) remains the right choice for
machines **without** the CLI and is documented as the alternative in D4 —
`enable` does not register it.

### 6.2 `agnes global disable`

Exact inverse, conservative: remove the user-scope enablement for `@agnes`
plugins (`claude plugin uninstall <p>@agnes --scope user`; project-scope
installs in the workspace are untouched), `claude mcp remove --scope user
agnes` **only** when the entry's command resolves to an `agnes mcp`
invocation, strip the marker-fenced CLAUDE.md block (rest of the file
preserved), remove the Agnes-marked user-level SessionStart hook entry
(matched via `_OUR_COMMAND_MARKERS`; foreign hook entries untouched), set
`global_scope: false`. Marketplace registration and clone
stay (they are also used by the workspace flow).

### 6.3 `agnes global status`

One row per artifact — marketplace registration, plugins (n of m manifest
plugins installed user-scope, version drift), MCP entry (present + binary
path exists), user-level SessionStart hook (present + canonical command),
rails block (present + byte-identical to the template shipped
with the running CLI version),
config flag — each `ok | missing | drifted`, with the repair hint
(`agnes global enable` re-runs convergence). `--json` for scripting.

### 6.4 User-scope write discipline

This design introduces a **new discipline** for user-scope files — it is
not a description of current practice, and stating it explicitly matters
because two existing writers diverge from it today: JSON owned by Claude
Code (`enabledPlugins` in `~/.claude/settings.json`, `mcpServers` in
`~/.claude.json`) is mutated only through the `claude` CLI
(`plugin install/uninstall`, `mcp add/remove`), never hand-edited. The
divergers: `refresh_marketplace.py::_enable_plugins_in_workspace_settings`
hand-writes `enabledPlugins` into the **cwd** workspace settings (made
target-aware and skipped in user mode by §7.1), and the Cowork
setup-bundle flow writes an `mcpServers` entry into user-level settings
directly (left as-is in v1; migration is follow-up hygiene, §12).

The one file this feature edits directly is `~/.claude/CLAUDE.md`
(markdown, marker splice) via a new `cli/lib/user_scope.py` helper that
follows the `automode.py` recovery philosophy — **on anything unexpected,
warn and leave the user's file untouched** (never back up + rebuild a
user-owned file): markers absent → append the block; exactly one
well-formed marker pair → replace its contents; duplicated or unmatched
markers → warn, skip, and let `agnes global status` report the block as
`drifted`. Writes are atomic (temp file + rename), matching
`cli/config.py::save_config`.

One further user-settings key is edited directly: `hooks` in
`~/.claude/settings.json` (no `claude` CLI exists for hook management).
That writer follows the workspace hook installer's contract
(`cli/lib/hooks.py`): only entries matching `_OUR_COMMAND_MARKERS` are
ever replaced or removed, third-party entries pass through untouched — but
combined with the automode-style recovery above (corrupt user file → warn
+ leave untouched, never rebuild).

## 7. D3 — convergence

### 7.1 `agnes refresh-marketplace --target user|project`

The reconcile trio — `_reconcile_with_manifest`,
`_list_installed_agnes_plugins_in_cwd`, **and**
`_enable_plugins_in_workspace_settings` (the function that performs the
actual `enabledPlugins` write, hardcoded to `Path.cwd()` today) — gains a
target parameter. Default stays `project` (workspace flow byte-for-byte
unchanged). In `user` mode:

- installs/updates/prunes run with the `claude` CLI's `--scope user`;
- the "installed" snapshot filters the same `claude plugin list --json`
  output by the per-entry `scope` field (`"user"`) instead of
  `projectPath == cwd`. Verified against a current Claude Code install:
  entries carry `scope: "user"|"project"` and user-scope installs have
  `projectPath: null`. The repo's existing test fixtures predate the
  `scope` field, so the filter must be defensive (`scope == "user"`, or
  `projectPath` null/absent on older CLIs);
- `_enable_plugins_in_workspace_settings` is **skipped entirely** —
  user-scope enablement is recorded by `claude plugin install --scope user`
  itself. This skip is what actually makes running convergence from an
  arbitrary cwd **safe**: without it, user-mode reconcile would still
  hand-write `enabledPlugins` into whichever repo it happens to run from,
  violating §8's guarantee (guard-tested in §10).

Flag naming: the CLI flag is `--target user|project`, deliberately **not**
`--scope` — the command-UX standard reserves `--scope` for the frozen
data-locality enumeration (`auto|local|server`), and a third meaning
(Claude Code install target, after `admin mcp`'s credential scope) would
erode the one-mental-model rule. The internal parameter may mirror the
`claude` CLI's own `--scope` argument it forwards to.

### 7.2 `agnes update` step `global`

After the existing `marketplace` step: when `global_scope: true` in config,
run (a) the user-scope reconcile (7.1), (b) re-assert the rails block
(template may have changed with the CLI version), (c) verify the MCP entry
(repair the binary path if the launcher moved). Reported like every other
step; a failure is recorded and never aborts the run. Flag off → step
skipped silently (no behaviour change for analysts).

This means the global layer is converged by every trigger of `agnes
update` — the user-level SessionStart hook (§7.3, installed by default),
the workspace SessionStart hook, and manual runs. Under `--no-hook`,
convergence happens only when the analyst workspace opens or on manual
runs — accepted for that opt-out.

### 7.3 User-level SessionStart hook (default ON; `--no-hook` opts out)

`agnes global enable` installs into `~/.claude/settings.json` a user-level
SessionStart entry with the same shape as the workspace one
(`bash -c "( nohup agnes update --quiet … & ) ; true"`), tagged by the
existing `_OUR_COMMAND_MARKERS` matching so `disable` (and idempotent
re-runs) strip exactly ours. Default ON is the product owner's decision
(§13) — every session in every repo keeps data, plugins, and the global
layer fresh. `--no-hook` skips the entry (and §7.2 describes the
convergence bound under that opt-out).

Safety argument, in order: `update` anchors to `workspace_root` (never
touches the repo it runs from); 7.1 makes the marketplace step
cwd-independent — which is why the hook and the `--target`-aware refresh
MUST ship in the same release, never the hook first; the single-instance
`update.lock` collapses the burst when the user opens five repos at once;
and user+project hooks merging means the workspace double-fires `update`,
which the lock also absorbs.

## 8. What stays workspace-scoped (privacy properties)

- **Transcript upload:** `agnes push` scans only the anchored workspace's
  Claude Code project folder. Sessions from the engineer's other repos are
  **never** uploaded, with or without the global layer, with or without
  the user-level hook. This is a property of the existing design that this spec
  deliberately preserves and documents.
- **Foreign repos are never written to.** No step in D1–D3 writes into any
  project directory other than the anchored workspace — including the
  user-mode reconcile, which skips today's cwd-based `enabledPlugins`
  writer (§7.1). The v1 acceptance checklist includes a guard test for
  this (§10).
- **Tokens:** nothing in D2 places a secret into any Claude Code config
  file. The stdio MCP server reads `~/.config/agnes/token.json` (0600) at
  runtime; the remote-MCP alternative documented in D4 uses OAuth (no PAT
  in config) and, where a header is unavoidable, documents only the
  `${VAR}`-expansion form — never a plaintext token.

## 9. D4 — docs: `docs/global-distribution.md`

One page, linked from `docs/README.md`, three audiences:

1. **Engineer (CLI machine):** `agnes init` (or existing workspace) →
   `agnes global enable` → restart sessions. What you get, how to check
   (`agnes global status`), how to remove.
2. **Engineer without a workspace / lightweight machine:** the remote MCP
   recipe — `claude mcp add --scope user --transport http agnes
   https://<agnes-host>/api/mcp/http` → OAuth consent; RBAC-filtered
   foundation tools, no local data.
3. **Operator (fleet default):** managed-settings recipe forcing
   `extraKnownMarketplaces` + `enabledPlugins` + a managed MCP entry for
   every engineer's machine, with `strictKnownMarketplaces` noted; placeholder
   hosts only.

Plus one pointer line in the root `CLAUDE.md` "Local sync & Claude Code
hooks" section.

## 10. Testing

- **Resolver matrix** (unit, `tests/test_workspace_resolve.py`): env set /
  unset × cwd shaped / unshaped × anchor present / stale / absent — assert
  the full precedence table, including "stale anchor → None". One dedicated
  assertion pins that `resolve_data_workspace` (cwd before anchor) and
  `update._resolve_workspace` (anchor before cwd) keep their
  **deliberately different** precedence, so a future dedup refactor cannot
  silently collapse them.
- **Adoption smoke:** `agnes query --local` and MCP `query_local` from a
  temp cwd with a fake anchored workspace (monkeypatched
  `AGNES_CONFIG_DIR`) succeed; with no anchor they render the typed hint.
  `agnes pull` with unshaped cwd + no anchor → typed error, no scaffold
  created in cwd (the **no-writes-to-foreign-repos guard**).
- **`global` group:** fake `$HOME` + `AGNES_CONFIG_DIR`, driving the
  `claude` CLI through the monkeypatched `subprocess.run` recorder +
  `shutil.which` fixtures that `tests/test_cli_refresh_marketplace.py`
  already provides (`recorder`, `claude_in_path` — no PATH shim binary):
  enable twice → identical end state, second run all no-ops; disable →
  byte-identical `~/.claude/CLAUDE.md` outside markers, foreign MCP entry
  named `agnes` survives with a warning; user CLAUDE.md with duplicated or
  unmatched markers → warned, left byte-identical, `status` reports the
  block `drifted`.
- **Reconcile target:** `--target user` never invokes `claude plugin` with
  `--scope project` (and vice versa); prune only ever targets `@agnes`
  plugins in the selected target; and the foreign-repo guard of §8: run the
  user-mode reconcile from a temp cwd and assert that cwd's `.claude/`
  is left untouched.
- **Update gating:** flag off → no `global` step side effects; on → step
  runs after `marketplace`, failure recorded without aborting.
- **Hook default:** `enable` installs the user-level SessionStart entry;
  `enable --no-hook` does not; `disable` removes exactly the Agnes-marked
  entry, leaving third-party user-level hooks byte-identical.
- No schema/DB change anywhere → no DuckDB↔PG parity or migration-ladder
  obligations (CLI + docs only).

## 11. Rollout & phasing

- **PR 1 — D1** (small, independently valuable): resolver + adoption +
  tests, including the `query_local`/`pull` docstring updates (§5.2) and a
  sweep of `docs/` + `--help` texts for descriptions of the old
  cwd-scaffold behavior. CHANGELOG *Changed*: data commands and the stdio
  MCP server fall back to the anchored workspace from any directory;
  *Fixed*: `agnes pull` no longer scaffolds a data tree into an arbitrary
  cwd.
- **PR 2 — D2 + D3 + D4:** `agnes global` group, refresh `--scope`,
  `update` step, docs page. CHANGELOG *Added*. Depends on PR 1 (the MCP
  entry registered without `--env` relies on the resolver).
- Release-cut per `docs/RELEASING.md` on whichever PR lands last with
  `[Unreleased]` content.

## 12. Future work (explicitly out of scope)

- Skills-bundle sync client (`/api/user/skills-bundle` →
  `~/.claude/skills/`) with its own convergence step.
- Server-rendered rails block (per-instance `/api/welcome?surface=global`)
  replacing the static template.
- Interactive nudge at the end of `agnes init` ("enable globally? [y/N]").
- Managed-settings *generator* (`agnes admin global-settings emit`) that
  renders the fleet JSON from the instance's marketplace + MCP config.
- Migrating the Cowork setup-bundle's direct `mcpServers` settings write to
  the `claude mcp add` path, aligning legacy code with the §6.4 discipline.

## 13. Resolved decisions (2026-07-31, product owner)

The draft's three open questions were decided:

1. **Plugin subset:** v1 installs *all* plugins from the caller's filtered
   manifest at user scope — no `--plugins` filter flag. The stack is the
   single curation surface; per-plugin opt-out is `claude plugin uninstall
   --scope user` or unsubscribing in the stack.
2. **User-level SessionStart hook:** installed **by default** by
   `agnes global enable`, with `--no-hook` as the opt-out (§7.3). This
   overrides the draft's opt-in lean; the hard prerequisite stands — the
   hook and the `--target`-aware refresh ship in the same release.
3. **Rails block:** the compact ~20-line block (§6.1 step 4), not a
   one-line pointer — deterministic rails are worth the ~200 tokens per
   session.
