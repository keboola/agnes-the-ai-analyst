# Clean analyst bootstrap — design

**Date:** 2026-05-04 (revision 5 — CLI binary renamed from `da` to `agnes` for branding consistency)
**Branch:** `zs/clean-analyst-bootstrap-spec`
**Status:** Draft (approved by user, pre-implementation)
**Successor to:** today's `da analyst setup` flow (interactive email/password) and the empty-folder bug under `da sync`.

**CLI binary rename:** As part of this rewrite the CLI binary changes from `da` to `agnes`. References to legacy command names (`da sync`, `da fetch`, `da analyst setup`, `da metrics`) keep their `da` prefix throughout this document — they're historical artifacts being removed. New commands use `agnes` (`agnes init`, `agnes pull`, `agnes push`, `agnes catalog`, …).

## Problem

A new analyst should be able to:

1. Sign in to the Agnes web UI.
2. Click a button on `/setup?role=analyst`, copy a single Claude-Code-paste prompt to the clipboard.
3. In an empty terminal, in an empty folder, paste the prompt into Claude Code.
4. Have Claude Code do **all** of the local setup — install the `agnes` CLI, trust the server's TLS cert (when needed), authenticate, generate `CLAUDE.md`, install Claude Code hooks, pull the RBAC-allowed parquets, build the local DuckDB views, write a human-readable workspace docs file.
5. Immediately start asking questions about the data — without ever typing a follow-up command.
6. From the second session onwards, have data freshness handled automatically by hooks (no `agnes pull` ever typed by hand).

Today this flow does not exist. The closest piece (`da analyst setup` in `cli/commands/analyst.py`) is interactive (prompts for email + password), produces a workspace layout that does not match what `da sync` later writes (the `data/parquet/`, `data/duckdb/`, `data/metadata/`, `user/artifacts/` directories it creates are never read by anything; `da sync` writes parquets to a sibling `server/parquet/` and DuckDB to `user/duckdb/analytics.duckdb`), and never registers the SessionStart/End hooks unless the analyst already managed to authenticate.

In addition, `da sync` itself creates empty directories (`.claude/rules/` is `mkdir`-ed before the bundle is fetched, so when the analyst has no rule grants the directory exists empty), and `da fetch` opens a DuckDB connection unconditionally, materializing an empty database file the first time the user runs `da fetch` before any `da sync`.

## Goals

- Single web → paste → done UX. Zero interactive prompts in the CLI bootstrap path.
- Workspace contains only files that something writes intentionally. No empty directories, no unread caches.
- Reader commands (`query`, `catalog`, `schema`, `describe`, `snapshot list`, `disk-info`, `status`, `diagnose`, `auth whoami`, `explore`) survive a freshly-bootstrapped workspace with zero table grants and zero corporate-memory rules without crashing.
- CLI verbs are mnemonic and non-overlapping. No two commands mean almost the same thing.
- The clean-install behavior is testable: integration tests that boot a real FastAPI server fixture, run the bootstrap end-to-end against `tmp_path`, and assert the exact set of files created.

## Non-goals

- Migration of existing analyst workspaces. This is a greenfield rewrite — no `data/parquet/` deprecation aliases, no `da analyst setup` shim that calls the new code. Any analyst running today's setup gets a different workspace shape; their old files are dead but harmless. Documented in CHANGELOG `**BREAKING**`.
- Admin CLI rewrite. The `/setup?role=admin` path keeps its current shape (TLS bootstrap, marketplace + plugins + skills + diagnose). Admin onboarding is unrelated.
- Offline initial bootstrap.
- PAT auto-refresh / refresh tokens.
- Multi-user / shared workspace.
- Web UI redesign. We add `?role=` query branching; no visual redesign.
- Layered per-workspace config (`<cwd>/.agnes/{config.yaml,token.json}`). Considered during brainstorming, dropped: multi-instance support is an edge case and the producer is undefined (no command currently creates `<cwd>/.agnes/`). Captured as a follow-up.

## Architecture overview

```
┌──────────────────────────────┐
│  Web /setup?role=analyst     │
│                              │   paste prompt
│  user logs in (web session)  ├────────────────────┐
│  clicks "Generate prompt"    │                    │
│  → POST /auth/tokens         │                    │
│    {scope:"bootstrap-analyst"│                    │
│     ttl_seconds:3600}        │                    │
│  → renders setup_instructions│                    │
│     .render(role="analyst")  │                    │
└──────────────────────────────┘                    │
                                                    ▼
                                       ┌──────────────────────────────┐
                                       │  Empty folder + Claude Code  │
                                       │                              │
                                       │  paste prompt; Claude runs:  │
                                       │   0. (TLS trust if needed)   │
                                       │   1. uv tool install <wheel>  # binary: agnes      │
                                       │   2. agnes init                 │
                                       │       --server-url URL       │
                                       │       --token PAT            │
                                       │   3. agnes catalog (smoke)      │
                                       └─────────────┬────────────────┘
                                                     │
                                                     ▼
                                ┌────────────────────────────────────────────┐
                                │ <cwd> — workspace, fully usable post-init  │
                                │ ├── CLAUDE.md          (Claude rails;      │
                                │ │     fetched from GET /api/welcome —     │
                                │ │     server-side render incl. admin       │
                                │ │     DB-stored override)                  │
                                │ ├── AGNES_WORKSPACE.md (human docs)        │
                                │ ├── .claude/                               │
                                │ │   ├── settings.json (model, perms,       │
                                │ │   │   hooks: SessionStart→`agnes pull`,     │
                                │ │   │          SessionEnd  →`agnes push`)     │
                                │ │   ├── CLAUDE.local.md (stub)             │
                                │ │   └── rules/km_*.md (only if non-empty)  │
                                │ ├── server/parquet/*.parquet               │
                                │ │      (only created if manifest non-empty)│
                                │ └── user/                                  │
                                │     ├── duckdb/analytics.duckdb            │
                                │     │      (always — DuckDB needs it)     │
                                │     ├── snapshots/*.parquet (lazy, ad-hoc) │
                                │     └── sessions/*.jsonl    (lazy, on push)│
                                └────────────────────────────────────────────┘

next session: SessionStart hook → agnes pull (incremental MD5)
              SessionEnd   hook → agnes push (sessions + CLAUDE.local.md)
```

Single source of truth for data path: `agnes pull`. `agnes init` is a thin orchestrator that does auth + writes templates + installs hooks + calls `agnes pull` once. No data-path code lives in `init`.

Single source of truth for the install prompt: `app/web/setup_instructions.py`. New `role: Literal["analyst", "admin"]` parameter branches the step list. TLS trust block is the only piece shared between the two roles.

Single source of truth for `CLAUDE.md` content: server-side `/api/welcome`. `agnes init` fetches the rendered text rather than rendering from a client-side template. This means admin-published overrides (DB-stored at `claude_md_template` table, exposed via `/api/admin/workspace-prompt-template`) automatically flow to all analysts. Server-side default template (`config/claude_md_template.txt` or equivalent rendering source) and any DB override **both** need their path strings updated as part of this PR — see "Migration of admin override" in Components.

Config and PAT live globally per user at `~/.config/da/{config.yaml,token.json}`. There is no per-workspace config in this design.

## New CLI surface

The CLI is rewritten with mnemonic, non-overlapping verbs. There are no backward-compat aliases. Today's `da analyst *`, `da sync`, `da sync --upload-only`, `da fetch` are removed; `da metrics list/show` folds into `agnes catalog --metrics`; `da metrics import/export/validate` move under `agnes admin`. `agnes skills list/show` survive as analyst discovery commands; bulk-install variants (none today, but spec refuses to add them) stay out.

```
WORKSPACE LIFECYCLE
  agnes init                         one-time workspace bootstrap (--server-url, --token, --force, --workspace)
  agnes pull                         refresh registered data (server → workspace)  [--quiet, --json, --dry-run]
  agnes push                         upload sessions + notes (workspace → server)  [--quiet, --json, --dry-run]
  agnes status                       what's in this workspace, when last synced

DATA QUERY
  agnes query "SELECT ..."           local DuckDB SQL (over server/parquet/* + user/snapshots/*)
  agnes query --remote "SELECT ..."  server-side BQ passthrough
  agnes explore <view>               interactive REPL over a single view
  agnes disk-info                    snapshot disk usage summary

DISCOVERY
  agnes catalog                      tables I have access to (RBAC-filtered)
  agnes catalog --metrics            list metric definitions (replaces da metrics list)
  agnes catalog --metrics --show <id>   show one metric definition (replaces da metrics show)
  agnes schema <table>               columns + types
  agnes describe <table>             sample rows
  agnes skills list                  list bundled CLI skill markdown documents
  agnes skills show <name>           print one skill's content

SNAPSHOTS (ad-hoc remote materialization)
  agnes snapshot create <table> --as <name> [--select ... --where ... --limit ... --order-by ... --estimate / --no-estimate --force]
  agnes snapshot list
  agnes snapshot drop <name>
  agnes snapshot refresh <name>      re-run the snapshot's saved query
  agnes snapshot prune               drop snapshots older than --older-than

AUTH + IDENTITY
  agnes auth login                   interactive login (browser flow). NOT called by agnes init.
  agnes auth import-token <PAT>      non-interactive
  agnes auth whoami
  agnes auth logout
  agnes auth token create / list / revoke   (today's location; unchanged by this PR)

HEALTH
  agnes diagnose                     health check (server + local)

ADMIN-ADJACENT (kept; not part of analyst flow)
  agnes admin metrics import         starter-pack import of metric definitions
  agnes admin metrics export         dump metric definitions to YAML
  agnes admin metrics validate       validate metric definitions
  agnes admin <other>                existing admin verbs continue unchanged
```

Removed:
- `da analyst setup`, `da analyst status` — `analyst` namespace had only one user role; replaced by top-level `agnes init` + `agnes status`.
- `da sync` (and `--upload-only`) — split into `agnes pull` + `agnes push`. Hook commands rename accordingly.
- `da fetch` — folded into `agnes snapshot create` with all flags carried over (`--select`, `--where`, `--limit`, `--order-by`, `--as`, `--estimate`, `--no-estimate`, `--force`).
- `da metrics list/show` — folded into `agnes catalog --metrics`.
- `da metrics import`, `da metrics export`, `da metrics validate` — relocated to `agnes admin metrics {import,export,validate}` (admin-only operations).

Surface decisions vs. earlier draft:
- `agnes skills list / show` retained for analyst-side discovery. Skills bundled under `cli/skills/*.md` (e.g., `agnes-data-querying.md`, `agnes-table-registration.md`) carry rails that the rebased main expanded as part of #160 (cost guardrail, registry-gating). Removing them would cost the analyst documentation that the project actively invests in. Bulk install/copy verbs are not added.
- `agnes auth token …` keeps its current location under `auth_app` (today's `cli/commands/auth.py:200-201` registers the sub-Typer there). No move to top-level `da token`. Surface listing reflects that.

Reader commands explicitly listed (`agnes explore`, `agnes disk-info`, `agnes snapshot refresh`, `agnes snapshot prune`, `agnes skills list/show`) survive unchanged.

## Components

### Server-side (Python, FastAPI)

| Component | File | Change |
|---|---|---|
| Analyst install-prompt branch | `app/web/setup_instructions.py` | Add `role: Literal["analyst","admin"]="admin"` to `resolve_lines()` and `render_setup_instructions()`. Analyst layout: TLS trust block (reused, when `ca_pem` supplied) → install `agnes` (reused) → `agnes init --server-url X --token Y --workspace .` → `agnes catalog` smoke verify → confirm. Drop for analyst: marketplace, plugins, skills, diagnose, login, whoami (all subsumed by `agnes init`). |
| `/setup?role=...` query branching | `app/web/router.py` `setup_page` (line 717) | Read `role` query param, default `"admin"`. Pass to `render_setup_instructions(role=...)`. Existing `/install` 302 redirect to `/setup` is preserved (legacy bookmarks keep working). |
| `setup.html` UI | `app/web/templates/setup.html` (or wherever `setup_page` renders) | Two role tiles: "Analyst workspace" / "Admin CLI". PAT mint button per tile, posts to `/auth/tokens` with `scope` matching the tile. Renders the prompt for the selected role. |
| PAT scope + TTL clamp | `app/api/tokens.py` (`CreateTokenRequest` Pydantic model + `create_token` route) | Add two fields: `scope: str = "general"` and `ttl_seconds: int \| None = None` (alongside the existing `expires_in_days: Optional[int] = 90` at lines 23-25). Resolution: when `ttl_seconds` is set, it wins; otherwise fall back to `expires_in_days`. **Upper bound:** mirror the existing `expires_in_days <= 3650` cap at line 100 with `ttl_seconds <= 315_360_000` (3650 days × 86400 s) so a hostile client can't bypass the cap by switching field names. For `scope == "bootstrap-analyst"`, server force-clamps the resolved TTL to ≤ 3600 s regardless of request. Audit-log entry includes the scope. The audit log is the only consumer of `scope` in this PR — per-endpoint enforcement is an explicit follow-up. |
| Server-side template rewrite | `config/claude_md_template.txt` (or wherever `render_claude_md` reads its default from) | Update path strings: `data/parquet/` → `server/parquet/`, `data/duckdb/...` → `user/duckdb/analytics.duckdb`. Replace `da sync` → `agnes pull`, `da fetch` → `agnes snapshot create`, `da metrics list` → `agnes catalog --metrics`. |
| Admin override migration | `claude_md_template` DB table (schema v23, exposed via `/admin/workspace-prompt` UI and `app/api/claude_md.py` admin CRUD) | Add a module-level constant `_LEGACY_STRINGS = ("data/parquet", "da sync", "da fetch", "da analyst setup", "da metrics list", "da metrics show")` and a helper `def _scan_legacy_strings(text: str) -> list[str]` inside `app/api/claude_md.py`. Add a `legacy_strings_detected: list[str] = []` field to `TemplateGetResponse` (today defined at `app/api/claude_md.py:72-76`); `admin_get_workspace_template` populates it via `_scan_legacy_strings(override.content)`. UI in `app/web/templates/admin_workspace_prompt.html` (file confirmed to exist) renders a yellow banner above the editor when the list is non-empty: "This override references CLI verbs / paths that were renamed in this release. Re-author and Save to clear the warning. See CHANGELOG for the rename list." Migration stays manual — admin re-authors and saves. |
| `/api/welcome` content unchanged | `app/api/claude_md.py:91` (`get_welcome`) | No code change — endpoint already serves rendered CLAUDE.md. Spec calls it out so implementer knows `agnes init`'s producer is here, not in the client. |
| Adopt `cli/error_render.py` (added in #160) for client-side errors | server: nothing — client-side only | `cli/error_render.py:render_error(status_code, body)` was introduced in 0.32.0 for typed BQ errors served by `agnes query --remote` (recognizes `detail.kind` / `detail.reason` shapes; falls back to plain HTTP `{code}: {text}`). The renderer is structurally generic — no BQ-specific code. `agnes init` and `agnes pull` are **first-time adopters in the bootstrap path** (today's `sync.py`, `auth.py`, `fetch.py` don't import it). Pattern: synthesize a `{"detail": {"kind": "...", "hint": "...", "message": "..."}}` dict client-side and pass with a chosen `status_code` (0 or `-1` for purely client-side errors with no HTTP origin), exactly as `cli/commands/query.py:152, 165` already does for `RemoteQueryError` translation. New typed kinds added in this PR: `auth_failed`, `server_unreachable`, `manifest_unauthorized`, `disk_full`, `partial_state` — the renderer doesn't gate on a kind allowlist, so no renderer change is needed. No server work; client-side only. |

### Client-side (CLI, Python)

| Component | File | Change |
|---|---|---|
| CLI binary rename | `pyproject.toml` (`[project.scripts]`), `cli/main.py` (`Typer(name=...)`) | Replace `da = "cli.main:app"` with `agnes = "cli.main:app"` in `pyproject.toml`. Replace `name="da"` with `name="agnes"` in the `Typer(...)` call at `cli/main.py:52`. No backward-compat alias shipped. Reinstall via `uv pip install -e ".[dev]"`. |
| `agnes init` (new) | `cli/commands/init.py` (new) | Required args: `--server-url`, `--token`. Optional: `--force`, `--workspace` (default `cwd`). Steps: (1) verify server reachability + PAT validity via `GET /api/catalog/tables` with `Authorization: Bearer <PAT>` — same endpoint `agnes auth import-token` already uses for this purpose (`cli/commands/auth.py:154`); exercises full PAT validation chain (revocation, expiry, hash) and 401s on bad PAT, unlike `/api/health` which is unauthenticated; (2) save server URL + PAT to `~/.config/da/{config.yaml,token.json}`; (3) `GET /api/welcome` and write its body to `<workspace>/CLAUDE.md`; (4) write `.claude/settings.json` (model, permissions, hooks pointing at `agnes pull` and `agnes push`) — delegate hook installation to `cli/lib/hooks.py:install_claude_hooks` (see new module row below); (5) write `.claude/CLAUDE.local.md` (stub, only if absent); (6) call `cli/lib/pull.py:run_pull(server_url, token, workspace)` programmatically (no Typer round-trip); (7) write `AGNES_WORKSPACE.md` from a static client-side template with `{created_at}`, `{server_url}`, `{workspace_path}` substituted. `agnes init` does NOT call `agnes auth login`; the PAT from the paste-prompt is the only auth path during bootstrap. Errors are rendered by `cli/error_render.py:render_error()` — `agnes init` synthesizes `{"detail": {"kind": "...", "hint": "..."}}` dicts client-side (pattern: `cli/commands/query.py:152, 165`); typed kinds: `auth_failed`, `server_unreachable`, `partial_state`, `disk_full`. |
| `cli/lib/pull.py` (new module) | `cli/lib/pull.py` + `cli/lib/__init__.py` (new) — establish `cli/lib/` as the shared-library tree | Pure-function refactor of today's `cli/commands/sync.py:sync()` body, minus Typer decorators and stdout. Signature: `def run_pull(server_url: str, token: str, workspace: Path, *, dry_run: bool = False) -> PullResult`. Returns a structured `PullResult` (tables_updated, parquets_total, rules_count, duration_s, errors). Caller decides what to print (`agnes init` summarizes; `agnes pull` Typer wrapper prints per `--quiet`/`--json` flags). Tested directly without subprocess. **Packaging:** `cli/lib/__init__.py` (empty file) is required for Hatchling to include the dir in the wheel — `pyproject.toml:packages` already lists `cli`, sub-packages with `__init__.py` are picked up automatically. |
| `cli/lib/hooks.py` (new module) | `cli/lib/hooks.py` (new) — replaces `cli/commands/analyst.py:_install_claude_hooks` | `def install_claude_hooks(workspace: Path) -> None`. Idempotent. Reads `<workspace>/.claude/settings.json`, drops any prior entry whose every command is a `agnes pull`/`da sync`/`agnes push` invocation (covers both today's hook commands and the new ones during a transition window if anyone runs the new init in a folder that had old hooks), appends fresh entries: `SessionStart → agnes pull --quiet 2>/dev/null \|\| true`, `SessionEnd → agnes push --quiet 2>/dev/null \|\| true`. Workspace-level scope (`<workspace>/.claude/settings.json`, not user-home), preserves third-party hooks. Lives next to `cli/lib/pull.py` under the new `cli/lib/__init__.py` package. |
| `agnes pull` (renamed from `da sync`) | `cli/commands/pull.py` (renamed from `cli/commands/sync.py`) | Behavior is today's `da sync` minus the `--upload-only` branch. Lazy-mkdir fixes (see below). Calls `cli/lib/pull.py:run_pull` and prints the result. Flags: `--quiet` (suppress success stdout, used by hook), `--json` (machine output of `PullResult`), `--dry-run` (compute deltas without writing — uses `dry_run=True`). Errors render via `cli/error_render.py`. |
| `agnes push` (extracted from `da sync --upload-only`) | `cli/commands/push.py` (new) | Uploads `user/sessions/*.jsonl` and `.claude/CLAUDE.local.md`. Lazy: skip when nothing to upload (no `user/sessions/` mkdir if no sessions). Same auth as `agnes pull`. Flags: `--quiet`, `--json`, `--dry-run`. Errors render via `cli/error_render.py`. |
| `agnes snapshot create` (renamed from `da fetch`) | `cli/commands/snapshot.py` | Move logic from `cli/commands/fetch.py` into a `create` subcommand of the existing `snapshot` group. Remove `cli/commands/fetch.py`. Carry over all flags: `--select`, `--where`, `--limit`, `--order-by`, `--as`, `--estimate`, `--no-estimate`, `--force`. Add existence check before opening DuckDB to avoid creating an empty DB file when no `agnes pull` has run yet (guard: `if not db_path.exists(): typer.echo("Local DuckDB not found. Run: agnes pull"); raise typer.Exit(1)`). Existing `agnes snapshot {refresh, prune, list, drop}` are unchanged. |
| `agnes status` (renamed from `da analyst status`) | `cli/commands/status.py` (renamed from analyst.py status fn) | Path refs updated to new layout: `server/parquet/`, `user/duckdb/analytics.duckdb`. Drop `data/metadata/last_sync.json`; use mtime on `user/duckdb/analytics.duckdb` as freshness proxy. |
| Lazy-mkdir contract | `cli/commands/pull.py`, `cli/lib/pull.py`, `cli/commands/push.py` | No `mkdir(parents=True, exist_ok=True)` before a conditional write loop. Mkdir only immediately before the first file write. Concretely: `_fetch_and_write_rules` mkdirs `.claude/rules/` only when `mandatory ∪ approved` is non-empty; `parquet_dir` mkdir is inlined into the per-table download loop. |
| `agnes catalog --metrics` flag | `cli/commands/catalog.py` | Add `--metrics` flag (replaces `da metrics list`) and `--metrics --show <id>` (replaces `da metrics show`). Decided shape, not unresolved — implementation should not negotiate. |
| `agnes admin metrics {import,export,validate}` (relocated) | `cli/commands/admin.py` | Add a `metrics` sub-Typer to the existing `admin_app` (which already nests sub-Typers `memory`, `group`, `grant`, `break-glass` per `cli/commands/admin.py:10`). Move `import`, `export`, `validate` from `cli/commands/metrics.py`. Admin-only; not part of analyst flow. |
| Removed (full delete) | `cli/commands/{metrics.py, fetch.py, analyst.py, sync.py}` | Deleted entirely (greenfield). |
| Retained | `cli/commands/skills.py` | Kept. `agnes skills list` and `agnes skills show` are analyst-side discovery commands. No code change in this PR. |

### Templates and docs

| Component | File | Change |
|---|---|---|
| Server-side `CLAUDE.md` template | `config/claude_md_template.txt` (and any DB override flagged in admin migration) | Path strings + verb names updated as listed in Server-side table. |
| `AGNES_WORKSPACE.md` template (new) | `config/agnes_workspace_template.txt` (new, client-side static asset bundled with the wheel) | Three placeholders: `{created_at}`, `{server_url}`, `{workspace_path}`. Header line uses all three; remaining content is static. Content described in dedicated section below. |
| Repo-root `CLAUDE.md` rewrite | `CLAUDE.md` (project root) | Update all references: `da sync` → `agnes pull`, `da analyst setup` → `agnes init`, `da metrics list/show` → `agnes catalog --metrics`, `da fetch` → `agnes snapshot create`, `data/parquet/` → `server/parquet/`. The "Local sync & Claude Code hooks" subsection and the "Querying Agnes data — agent rails" subsection both need full walk-throughs. The latter was expanded by 0.32.0 (#160) with cost-guardrail / registry-gating prose — those sections stay verbatim, just verb-renamed. The "Business Metrics" subsection's `da metrics import` / `da metrics list` / `da metrics show` examples become `agnes admin metrics import` and `agnes catalog --metrics` respectively. |

## Web UI flow

```
GET /setup?role=analyst
   └─ render setup.html with `role=analyst` context
      ├─ "Analyst workspace" tile is active (visual highlight)
      ├─ "Admin CLI" tile linked to /setup?role=admin

[user clicks "Generate prompt"]
   └─ JS: POST /auth/tokens
        body: {scope: "bootstrap-analyst", ttl_seconds: 3600, name: "init"}
        auth: existing web session cookie
      ├─ server mints PAT, force-clamps TTL ≤ 3600
      ├─ audit_log entry: scope, expires_at
      └─ returns {pat: "agnes_pat_..."}

   └─ JS: render setup_instructions in clipboard text
        substitutes {token}, {server_url}; copies to clipboard

[user pastes into Claude Code in empty folder]
   └─ Claude executes the steps in order
```

PAT lifecycle: minted on click, single TTL window (1 h), no auto-revoke after first use. The user has 1 h to retry the bootstrap if a step fails. After 1 h they re-click "Generate prompt" for a new PAT. The `scope="bootstrap-analyst"` claim is informational at this stage — its only consumer in this PR is the server's audit log. Per-endpoint scope enforcement is a follow-up issue.

Legacy `/install` URL: kept as a 302 redirect to `/setup`. All new references in code and docs use `/setup?role=...`.

## Workspace layout

Post-`agnes init`, workspace contains exactly:

```
<cwd>/
├── CLAUDE.md
├── AGNES_WORKSPACE.md
├── .claude/
│   ├── settings.json
│   └── CLAUDE.local.md
└── user/
    └── duckdb/
        └── analytics.duckdb
```

Conditional additions:

- `./.claude/rules/km_*.md` — only when `/api/memory/bundle` returns ≥ 1 mandatory or approved item.
- `./server/parquet/<table>.parquet` — only when `/api/sync/manifest` returns ≥ 1 table the user has grants on.
- `./user/snapshots/<name>.parquet` — only after the user runs `agnes snapshot create <table> --as <name>`.
- `./user/sessions/<id>.jsonl` — only after the SessionEnd hook runs `agnes push` against captured Claude Code sessions.

Forbidden under any circumstances (these are the dead paths today's setup creates):

- `./data/parquet/`, `./data/duckdb/`, `./data/metadata/`, `./user/artifacts/` — none of these were read by any code path; removed entirely.
- `./.agnes/` — per-workspace config is out of scope for this PR.

## Data flow / sequence

```
Empty folder + Claude Code with paste prompt
│
├─ Step 0 (TLS trust block) — only when server uses private CA
│   writes ~/.agnes/{ca.pem, ca-bundle.pem}, appends shell rc block
│
├─ Step 1 — uv tool install <wheel>  # binary: agnes
│   writes ~/.local/bin/agnes
│
├─ Step 2 — agnes init --server-url URL --token PAT --workspace .
│   ├─ verify: GET /api/catalog/tables with Bearer PAT → 200 (PAT-validating endpoint)
│   ├─ save: ~/.config/da/{config.yaml, token.json}
│   ├─ fetch: GET /api/welcome?server_url=<URL>  → write ./CLAUDE.md
│   │      (passing the analyst-facing URL explicitly so behind-proxy
│   │       installs render the operator-visible URL, not the FastAPI
│   │       internal hostname; endpoint default falls back to
│   │       request.base_url which equals --server-url in practice)
│   ├─ write: ./.claude/settings.json (with hooks SessionStart→`agnes pull`, SessionEnd→`agnes push`)
│   ├─ write: ./.claude/CLAUDE.local.md (stub, if absent)
│   ├─ call:  agnes pull (programmatic — calls cli/lib/pull.py:run_pull)
│   │   ├─ GET /api/sync/manifest → {tables, hashes}
│   │   ├─ for each table where local md5 ≠ remote md5:
│   │   │   GET /api/data/<id>/download (stream)
│   │   │   if first table: mkdir ./server/parquet/ then write
│   │   ├─ rebuild DuckDB:
│   │   │   open ./user/duckdb/analytics.duckdb (creates if absent)
│   │   │   DROP all VIEW; CREATE VIEW <name> AS read_parquet(...) for each parquet
│   │   └─ GET /api/memory/bundle → {mandatory, approved}
│   │       if non-empty: mkdir ./.claude/rules/ then write km_*.md
│   │       if empty: skip mkdir
│   └─ write: ./AGNES_WORKSPACE.md (from client-side static template)
│
├─ Step 3 — agnes catalog (smoke verify)
│   confirms end-to-end works; prints table count
│
└─ Step 4 — confirm
    Claude reports: tables synced, files created, hooks active.

Subsequent sessions:
├─ SessionStart hook fires: agnes pull --quiet 2>/dev/null || true
├─ user works
└─ SessionEnd hook fires:   agnes push --quiet 2>/dev/null || true
```

## Empty-folder discipline

The clean-install contract has two halves: writers must be lazy (don't pre-allocate empty dirs); readers must be tolerant (don't crash on missing dirs).

### Writer contract

> Every writer MUST mkdir its target *immediately before* the first file write, never as bulk pre-allocation. If 0 files are written, no mkdir happens.

Concretely:

| Writer | File:line (today) | Today | After |
|---|---|---|---|
| `_fetch_and_write_rules` | `cli/commands/sync.py:222` | `rules_dir.mkdir(parents=True, exist_ok=True)` before iterating | Check `mandatory + approved` first; if empty, return without mkdir. |
| Per-table download loop | `cli/commands/sync.py:120, 529` | `parquet_dir.mkdir(parents=True, exist_ok=True)` before loop | Mkdir inlined into the per-file write block; first table triggers mkdir. |
| `install_claude_hooks` | `cli/lib/hooks.py` (new; replaces `cli/commands/analyst.py:_install_claude_hooks`, today at line 254) | mkdir `.claude/` | unchanged — `.claude/` always has content (settings.json is load-bearing). Function lifted from the deleted `cli/commands/analyst.py` into a shared library so `agnes init` (and any future caller) can use it without importing the deleted module. |
| `_rebuild_duckdb_views` | `cli/commands/sync.py:321` | mkdir `user/duckdb/` | unchanged — DuckDB file is opened unconditionally as part of view rebuild; the file is the load-bearing artifact, not just the directory. |
| `agnes push` upload | (new) `cli/commands/push.py` | (n/a) | Mkdir `user/sessions/` only inside the per-session-write branch; `agnes push` with nothing to upload exits 0 without touching disk. |
| `agnes snapshot create` parquet write | `cli/commands/snapshot.py` | mkdir `user/snapshots/` before write | unchanged (snapshot create is the canonical writer; mkdir on first write is correct). |

### Reader contract

> Every reader MUST handle missing paths gracefully. "Gracefully" means:
> - **Exit 0 with empty / zero output** when missing paths are a natural empty answer (`agnes disk-info` shows 0; `agnes status` shows "initialized: no").
> - **Exit 1 with friendly hint** when the missing path means a workflow precondition isn't met (`agnes query`: "Local DuckDB not found. Run: agnes pull").
> - **Never create the path side-effect-ally** unless this command is the canonical writer for it.

Audit of current readers (only commands that touch the filesystem are listed; others are server-API only and unaffected):

| Command | Path it reads | Today's behavior | Change needed |
|---|---|---|---|
| `agnes query` | `user/duckdb/analytics.duckdb` | `.exists()` check, friendly "Run: da sync" exit 1 | Update hint text → "Run: agnes pull". |
| `agnes explore` | same | `.exists()` check, friendly exit | Update hint text. |
| `agnes snapshot create` (was `da fetch`) | same | unconditional `duckdb.connect()` → creates empty DB | Add `.exists()` check + hint "Run: agnes pull first". |
| `agnes snapshot create` (write side) | `user/snapshots/` | unchanged (writer, mkdir at first write) | unchanged. |
| `agnes disk-info` | `user/snapshots/` | `.exists()` guards around sum/count/free | unchanged. |
| `agnes snapshot list` | `user/snapshots/` | glob safe on missing | unchanged (glob returns empty iterator on missing dir). |
| `agnes snapshot refresh` / `prune` | `user/snapshots/` | glob/.exists() guards | unchanged. |
| `agnes push` | `user/sessions/` | `.exists()` check before iterating | unchanged. |
| `agnes status` | `server/parquet/`, `user/duckdb/...` | path strings reference legacy `data/parquet/` etc. | Update path strings; `.exists()` checks already in place. |

### Regression guard (test)

```python
def assert_no_dead_dirs(workspace: Path):
    """Workspace must not contain pre-allocated empty directories."""
    forbidden_unconditional = ["data/parquet", "data/duckdb", "data/metadata",
                               "user/artifacts", ".agnes"]
    for d in forbidden_unconditional:
        assert not (workspace / d).exists(), f"forbidden dir created: {d}"

    # Conditionally-empty dirs: present only if non-empty.
    for d in [".claude/rules", "server/parquet", "user/sessions",
              "user/snapshots"]:
        path = workspace / d
        if path.exists():
            assert any(path.iterdir()), f"{d} exists but is empty"
```

This guard runs in every clean-install integration test.

## `AGNES_WORKSPACE.md` content

Generated by `agnes init` in the workspace root from a static client-side template (`config/agnes_workspace_template.txt`, bundled with the wheel). Not state — pure documentation. Idempotent overwrite on every `agnes init` (preserves nothing, regenerates everything).

Three placeholders only: `{created_at}`, `{server_url}`, `{workspace_path}`. Used in the header line "Created: {created_at} · Server: {server_url} · Workspace: {workspace_path}". No email, no user identity, no role. Email is not used anywhere in the analyst CLI flow; PAT identifies the user server-side, and decoded JWT email is informational at best — we drop it from this header for clarity.

Sections:

1. **Header** — `Created: <ISO timestamp> · Server: <URL> · Workspace: <abs path>`.
2. **What's installed (global, per-user)** — table of paths in `~/.local/bin/`, `~/.config/da/`, `~/.agnes/`, shell rc block. Each row: `path | what it is | how to remove`.
3. **What's in this folder** — table of paths in workspace. Each row: `path | what it is`. Notes which dirs are conditional ("only when grants/sessions/etc. exist").
4. **How it stays fresh** — explains SessionStart/End hooks: what they run, when, what failure looks like (silent, `|| true`).
5. **Cheat sheet** — `agnes pull`, `agnes catalog`, `agnes query`, `agnes snapshot create`, `agnes status`, `agnes init --force` examples.
6. **Uninstall** — step-by-step recipe to remove the CLI globally, the config dir, the trust artifacts, the rc block, and the workspace itself.

Approximate size: 3.5 KB, ~100 lines. Disk overhead: nil.

The content is written at the human, not at Claude. `CLAUDE.md` is for the AI; `AGNES_WORKSPACE.md` is for the person reading `ls`.

PAT value never appears in `AGNES_WORKSPACE.md` — only its location (`~/.config/da/token.json`). The token file is `chmod 600`.

## Error handling

| Failure | Detection | Behavior |
|---|---|---|
| Server unreachable during `agnes init` | `httpx.ConnectError` on `/api/catalog/tables` | exit 1 via `cli/error_render.render_error()` with kind `server_unreachable`, hint: "Cannot reach `<URL>` — check network or server status". |
| PAT expired | `/api/catalog/tables` → 401 | exit 1 via `render_error()` with kind `auth_failed`, hint: "Token expired — get a fresh one at `<URL>/setup?role=analyst`". |
| PAT invalid (mis-paste) | 401, JWT decode failure | exit 1 via `render_error()` with kind `auth_failed`, hint: "Token format invalid — re-copy from `/setup`". |
| TLS trust failure | curl/wheel install fails with `unknown CA` | exit 1, hint refers user back to paste-prompt step 0. |
| Disk full during `agnes pull` | `OSError(ENOSPC)` on parquet write | atomic rename → partial file deleted; exit 1 with disk-info dump. |
| Concurrent `agnes init` in same folder | sentinel `<cwd>/.claude/.init.lock` | second invocation: "Setup already running" exit 1. |
| Partial state (previous `agnes init` crashed mid-way) | `CLAUDE.md` exists but `.claude/settings.json` missing | `agnes init` (without `--force`): friendly hint "Workspace partially set up — run `agnes init --force` to redo". |
| `agnes pull` 401 mid-session (PAT revoked server-side) | response 401 from `/api/sync/manifest` | hook command prints warning, exits 0 (`\|\| true`); session continues with last-known data. Manual `agnes pull` next time prints actionable hint. |
| Empty manifest | `/api/sync/manifest` → `{"tables": []}` | success, no parquet dir created, no warning (valid state). |
| Empty memory bundle | `/api/memory/bundle` → `{"mandatory": [], "approved": []}` | success, no `.claude/rules/` dir (valid state). |
| Per-table 5xx mid-pull | per-table 500 from `/api/data/<id>/download` | per-table warn; pull continues; final exit 0 if at least one table succeeded, exit 1 if all failed. |
| Workspace path with spaces / unicode | path passed to subprocess as `cwd=`, no shell interpolation | works as-is; tested in clean-install integration test. |
| Hook fires in unrelated Claude Code session | settings.json is workspace-scoped (`<cwd>/.claude/settings.json`) | hook does not fire; Claude Code reads settings only for the directory it was opened in. |

Principle: hooks always end `\|\| true` so they never block a session. Manual commands are exit-1-with-hint. No silent failures in interactive flow.

## Verification / testing

Verification has three layers: (a) automated reader-smoke matrix that proves no command crashes on a freshly-bootstrapped workspace; (b) automated clean-install integration tests that prove the workspace contains exactly the expected files; (c) a manual end-to-end protocol that runs the actual paste prompt against a real local server.

### Test fixtures

| Fixture | Returns | What it pre-seeds |
|---|---|---|
| `fastapi_test_server` | object with `.url`, `.shutdown()` | Starts the FastAPI app in a background thread/subprocess against a `tmp_path`-rooted DATA_DIR. Clean schema (latest version, currently v23), two seeded users (`admin@example.com`, `analyst@example.com`, both with a known test password seeded into the local password provider), two seeded user groups (`Admin`, `Everyone`), three seeded tables in `table_registry` with one `query_mode='local'`, one `query_mode='materialized'`, one `query_mode='remote'`. Manifest + memory + welcome endpoints serve real (test) data. |
| `test_pat` | string PAT for `analyst@example.com` | Group membership: `Everyone` only. `resource_grants` for the local + materialized tables (so manifest returns 2 rows for them). Two `mandatory` corporate-memory items granted via group. PAT TTL: 1 h. |
| `test_pat_no_grants` | string PAT for `analyst@example.com` | Same user, but `resource_grants` is empty and `corporate_memory` has zero items granted to `Everyone`. Manifest returns `{"tables": []}`; memory bundle returns `{"mandatory": [], "approved": []}`. |
| `zero_grants_workspace` | `tmp_path` after running `agnes init --token <test_pat_no_grants> --server-url <fastapi_test_server.url>` | A fully-bootstrapped workspace where every conditional dir is absent. Used by the reader smoke matrix. The fixture also exposes a sentinel constant `NONEXISTENT_TABLE = "__nonexistent__"` for tests that need a deliberately-unknown table id; readers must produce a friendly exit-1 (no traceback) when given this id. |
| `web_session` | authenticated `httpx.Client` with cookies | Calls `POST /auth/password/login/web` with form fields `email=admin@example.com` and `password=<test_password>` (the test password is seeded into the same `users` row by `fastapi_test_server`). The form-login endpoint sets the session cookie that `POST /auth/tokens` requires (PAT mint route gates on `require_session_token`, see `app/api/tokens.py:88`). Used to mint PATs in PAT-scope tests. Choice rationale: real-endpoint login over dependency-override keeps the auth path under test rather than bypassed. |
| `client` | `TestClient(app)` | Plain FastAPI test client with no auth. Used for endpoint-shape tests. |

Fixtures live in `tests/conftest.py` (existing) plus a new `tests/fixtures/analyst_bootstrap.py`.

The autouse fixture `_reset_module_caches` in `tests/conftest.py:50-83` (added in 0.32.0 / #160 / commit `9ecbfd2a`) resets `app.instance_config._instance_config`, `connectors.bigquery.access.get_bq_access` lru cache, and `app.api.v2_quota._quota_singleton` between tests on the same xdist worker. The new bootstrap fixtures rely on this to keep `fastapi_test_server` invocations independent — no manual cache resets needed in test bodies.

### 5.1 Reader smoke matrix (automated)

```python
@pytest.mark.parametrize("cmd", [
    ["agnes", "catalog"],
    ["agnes", "catalog", "--metrics"],
    ["agnes", "schema", "__nonexistent__"],
    ["agnes", "describe", "__nonexistent__"],
    ["agnes", "query", "SELECT 1"],
    ["agnes", "explore", "__nonexistent__"],
    ["agnes", "disk-info"],
    ["agnes", "snapshot", "list"],
    ["agnes", "snapshot", "create", "__nonexistent__", "--as", "x", "--estimate"],
    ["agnes", "status"],
    ["agnes", "diagnose"],
    ["agnes", "auth", "whoami"],
    ["agnes", "skills", "list"],
    ["agnes", "skills", "show", "agnes-data-querying"],
])
def test_reader_does_not_crash_on_zero_grants(zero_grants_workspace, cmd):
    """No reader should crash with a Python traceback on a fresh
    workspace where the user has zero table grants and zero rules."""
    result = subprocess.run(cmd, cwd=zero_grants_workspace,
                            capture_output=True, text=True)
    assert result.returncode in (0, 1), \
        f"{cmd} crashed: rc={result.returncode}, stderr={result.stderr}"
    assert "Traceback" not in result.stderr, f"{cmd} threw: {result.stderr}"
```

This is the load-bearing test for "nothing crashes on missing dirs".

### 5.2 Clean-install integration tests

```python
def test_clean_install_minimal_grants(fastapi_test_server, tmp_path, test_pat):
    """User has 2 table grants + 2 mandatory rules → expected workspace shape."""
    subprocess.run(
        ["agnes", "init", "--server-url", fastapi_test_server.url,
         "--token", test_pat, "--workspace", str(tmp_path)],
        check=True,
    )
    # Required:
    for must in ["CLAUDE.md", "AGNES_WORKSPACE.md",
                 ".claude/settings.json", ".claude/CLAUDE.local.md",
                 "user/duckdb/analytics.duckdb"]:
        assert (tmp_path / must).exists(), f"missing required: {must}"
    # Conditional (present because grants/rules exist):
    assert len(list((tmp_path / "server" / "parquet").glob("*.parquet"))) == 2
    assert len(list((tmp_path / ".claude" / "rules").iterdir())) == 2
    # Forbidden:
    assert_no_dead_dirs(tmp_path)
    # Hooks installed correctly:
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert any("agnes pull" in h["hooks"][0]["command"]
               for h in settings["hooks"]["SessionStart"])
    assert any("agnes push" in h["hooks"][0]["command"]
               for h in settings["hooks"]["SessionEnd"])
    # CLAUDE.md was fetched from /api/welcome (not local template):
    claude_md = (tmp_path / "CLAUDE.md").read_text()
    assert "agnes pull" in claude_md and "da sync" not in claude_md  # post-rewrite content
    # AGNES_WORKSPACE.md content asserts (security + placeholder substitution):
    workspace_md = (tmp_path / "AGNES_WORKSPACE.md").read_text()
    assert test_pat not in workspace_md, "PAT must not leak into AGNES_WORKSPACE.md"
    assert "{created_at}" not in workspace_md, "placeholder not substituted"
    assert "{server_url}" not in workspace_md, "placeholder not substituted"
    assert "{workspace_path}" not in workspace_md, "placeholder not substituted"
    assert fastapi_test_server.url in workspace_md
    assert str(tmp_path) in workspace_md
    assert "agnes pull" in workspace_md  # cheat sheet uses new verb


def test_clean_install_zero_grants(fastapi_test_server, tmp_path, test_pat_no_grants):
    """User has 0 grants, 0 rules → minimal workspace, zero dead dirs."""
    subprocess.run(["agnes", "init", ...], check=True)
    must_exist = {"CLAUDE.md", "AGNES_WORKSPACE.md",
                  ".claude/settings.json", ".claude/CLAUDE.local.md",
                  "user/duckdb/analytics.duckdb"}
    must_not_exist = {".claude/rules", "server/parquet", "data/parquet",
                      "data/duckdb", "data/metadata", "user/artifacts",
                      "user/sessions", "user/snapshots", ".agnes"}
    for p in must_exist:
        assert (tmp_path / p).exists()
    for p in must_not_exist:
        assert not (tmp_path / p).exists()
    assert_no_dead_dirs(tmp_path)


def test_setup_force_preserves_user_files(...):
    """`agnes init --force` regenerates CLAUDE.md and AGNES_WORKSPACE.md
    but never touches CLAUDE.local.md."""

def test_readers_in_pre_setup_dir(tmp_path, test_pat):
    """User runs reader commands in a folder that never had `agnes init`.
    No crash; friendly hints to run init or pull."""
```

### 5.3 Server-side render test

```python
def test_render_setup_instructions_analyst_role():
    text = render_setup_instructions(
        server_url="https://agnes.example.com",
        token="agnes_pat_TEST",
        role="analyst",
    )
    assert "uv tool install" in text
    assert "agnes init" in text
    assert "--token" in text and "agnes_pat_TEST" in text
    assert "--server-url" in text
    assert "agnes catalog" in text
    # Must not contain (admin-only):
    assert "marketplace" not in text
    assert "claude plugin install" not in text
    assert "agnes skills" not in text
    assert "agnes diagnose" not in text
```

### 5.4 PAT scope/TTL test

```python
def test_bootstrap_pat_ttl_clamped_to_one_hour(client, web_session):
    resp = web_session.post("/auth/tokens", json={
        "scope": "bootstrap-analyst",
        "ttl_seconds": 86400,  # ignored — server force-clamps
        "name": "init",
    })
    assert resp.status_code == 200
    pat = resp.json()["pat"]
    payload = jwt.decode(pat, options={"verify_signature": False})
    assert payload["scope"] == "bootstrap-analyst"
    assert payload["exp"] - payload["iat"] <= 3600 + 5

def test_bootstrap_pat_falls_back_to_expires_in_days(web_session):
    """When ttl_seconds is omitted, expires_in_days still works (back-compat)."""
    resp = web_session.post("/auth/tokens", json={"name": "test", "expires_in_days": 30})
    assert resp.status_code == 200
    payload = jwt.decode(resp.json()["pat"], options={"verify_signature": False})
    assert payload["exp"] - payload["iat"] <= 30 * 86400 + 5
```

### 5.5 Manual clean-install protocol (pre-merge)

1. `git clean -fdx` in the repo (no build artifacts).
2. Boot FastAPI locally against a clean test instance state.
3. Empty terminal in `/tmp/test-analyst-1`. From the web `/setup?role=analyst`, paste prompt.
4. `tree -a /tmp/test-analyst-1` and compare with the expected tree from §5.2.
5. `claude` in that folder. Three queries: "what tables can I see", "SELECT count(*) FROM <t>", "show me last 5 rows of <t>". All must work without further intervention.
6. `/exit`. Verify SessionEnd hook ran (server-side audit log shows `agnes push`; `du -sh /tmp/test-analyst-1/user/sessions/` non-empty).
7. Second `claude` in same folder. Verify SessionStart hook fires (`agnes pull` request in audit log).
8. Second workspace `/tmp/test-analyst-2` with the same PAT (within TTL). Repeat 3-5. Verify global `~/.config/da/` is not duplicated; the second workspace has its own DuckDB.

This protocol is documented in `docs/RELEASE_CHECKLIST.md` as a mandatory pre-merge step for changes touching the bootstrap path.

## Out of scope

1. **Admin CLI tooling** — `/setup?role=admin` and `agnes admin *` continue unchanged. The new CLI surface listing in this spec is the *analyst* surface; admin verbs not listed (e.g., `agnes admin marketplace`, `agnes admin user`, etc.) are unaffected.
2. **Migration of existing analyst workspaces** — greenfield; old `data/parquet/` etc. are dead but harmless.
3. **Backward-compat aliases** — no `da analyst setup` → `agnes init` shim, no `da sync` → `agnes pull` shim. Hard cutover.
4. **Multi-user / shared workspace** — `<cwd>` is single-user.
5. **Offline initial bootstrap** — `agnes init` requires server reachability.
6. **PAT auto-refresh / refresh tokens** — bootstrap PAT expires after 1 h; user re-clicks "Generate prompt".
7. **Per-endpoint PAT scope enforcement** — `bootstrap-analyst` scope is informational at this stage (audit-trail). Per-endpoint enforcement is a follow-up issue.
8. **Web UI redesign** — `/setup?role=...` reuses the existing page shell + JS. No visual redesign.
9. **CLI rename adjacent commands** beyond what's listed (e.g., `agnes auth login` → `da login`) — out of scope.
10. **Layered per-workspace config** — `<cwd>/.agnes/{config.yaml,token.json}` overrides considered but dropped from this PR (no defined producer; multi-instance is edge case). Captured in Open questions.

## Open questions / follow-ups

- **Per-endpoint PAT scope enforcement** — should `scope="bootstrap-analyst"` PATs be restricted to `/api/catalog/tables`, `/api/sync/manifest`, `/api/data/*/download`, `/api/memory/bundle`, `/api/welcome` only, and refused on (e.g.) `/api/admin/*`? Today not enforced. New issue.
- **Layered per-workspace config** — supporting multi-instance use cases (one analyst, two Agnes servers) requires a defined producer for `<cwd>/.agnes/`. Options: `agnes init --per-workspace-config` flag, post-init manual `mkdir`, or `da config init`. Not chosen because no current user has asked for it. New issue if/when needed.
- **`agnes snapshot create --where` SQL flavor** — keep BigQuery flavor (today's `da fetch`) for parity with `agnes query --remote`, since BQ is the only remote source. Confirmed in this PR; flagged in case a non-BQ remote source is added later.
- **Hook performance budget** — `agnes pull` on a 1.1 GB workspace (real-world example: today's `tmp_oss/server/parquet/`) with all parquets unchanged should complete the manifest comparison in well under 1 s so SessionStart doesn't perceptibly delay the user. If incremental MD5 comparison is too slow at scale, consider a server-side ETag.
- **Anti-coupling test** — add a test that imports every `cli/commands/*.py` and `cli/lib/*.py` module and asserts no `cli/commands/*` module imports another `cli.commands.*` module except via dispatch (Typer subcommand registration). `cli/lib/*` modules may be imported by command modules; reverse direction (`cli.lib` importing `cli.commands`) is forbidden. Prevents `init` accidentally re-importing `pull`'s Typer wrapper instead of the library function.

## CHANGELOG entry (preview)

```markdown
## [Unreleased]

### Changed
- **BREAKING** CLI binary renamed from `da` to `agnes`. No backward-compat alias is shipped. Update shell aliases, hook commands in any pre-existing `.claude/settings.json`, scripts, and cron jobs. Reinstall via `uv tool install <wheel>`; the wheel now ships an `agnes` entry point.
- **BREAKING** Analyst bootstrap rewritten end-to-end. `da analyst setup` is removed; replaced by `agnes init` (non-interactive, requires `--server-url` and `--token`). `da sync` is split into `agnes pull` (refresh) and `agnes push` (upload). `da fetch` is folded into `agnes snapshot create`. `da metrics list/show` is folded into `agnes catalog --metrics`; `da metrics import/export/validate` move to `agnes admin metrics {import,export,validate}`. The `da analyst` namespace is removed; the workspace status command is now `agnes status`. The previous `da status` (server-health overview) becomes `agnes diagnose system`.
- **BREAKING** Workspace layout simplified. Removed: `data/parquet/`, `data/duckdb/`, `data/metadata/`, `user/artifacts/`. Canonical paths: `server/parquet/` (synced parquets), `user/duckdb/analytics.duckdb` (DuckDB views), `user/snapshots/` (ad-hoc snapshots), `user/sessions/` (recorded sessions).
- The `/setup` web page now branches on a `role` query parameter: `/setup?role=analyst` renders the analyst workspace bootstrap prompt; `/setup?role=admin` renders the admin CLI install prompt. `/install` continues to 302 to `/setup`.
- `CLAUDE.md` server-side template + repo-root `CLAUDE.md` updated to reference the new CLI verbs and workspace paths. The admin UI for the `claude_md_template` DB override (`/admin/workspace-prompt`) renders a yellow banner when the saved override contains legacy strings (`data/parquet/`, `da sync`, `da fetch`, `da analyst setup`, `da metrics list/show`); admins re-author and save to clear it. Migration is manual.

### Added
- `AGNES_WORKSPACE.md` — human-readable workspace docs file generated by `agnes init` in the workspace root. Documents global install, workspace layout, hooks, cheat sheet, uninstall recipe.
- PAT request body now accepts `scope: str = "general"` and `ttl_seconds: int | None = None` fields. PATs minted with `scope="bootstrap-analyst"` are TTL-clamped to ≤ 1 h server-side. Existing `expires_in_days` field continues to work; `ttl_seconds` wins when both are set.
- `cli/lib/` shared-library tree, with `cli/lib/pull.py:run_pull` (data-refresh primitive callable from both the Typer wrapper and `agnes init`) and `cli/lib/hooks.py:install_claude_hooks` (workspace-scoped Claude Code hook installer).

### Fixed
- `agnes pull` (formerly `da sync`) no longer creates `.claude/rules/` when the corporate-memory bundle is empty.
- `agnes pull` no longer creates `server/parquet/` when the manifest is empty.
- `agnes snapshot create` (formerly `da fetch`) no longer materializes an empty `user/duckdb/analytics.duckdb` when run before any `agnes pull`.
- Workspace `agnes status` reads from the canonical `server/parquet/` and `user/duckdb/analytics.duckdb` paths (was reading legacy `data/parquet/`, `data/metadata/last_sync.json`).
- `agnes init` and `agnes pull` errors now use the `cli/error_render.py` typed-error renderer (added in 0.32.0), so analyst-facing error UX matches the structured shape `agnes query --remote` already produces.

### Removed
- `da analyst setup`, `da analyst status`, `da sync`, `da fetch`. See "Changed" above for replacements.
- `da metrics` namespace as a top-level group (subcommands moved to `agnes catalog --metrics` for read-only views and `agnes admin metrics …` for write operations).
- Legacy workspace directories `data/parquet/`, `data/duckdb/`, `data/metadata/`, `user/artifacts/`. Existing analyst workspaces should be reinitialized with `agnes init --server-url ... --token ... --force` (a fresh empty folder is recommended).

### Kept (clarified)
- `agnes skills list` and `agnes skills show` survive as analyst-side discovery commands. Earlier draft proposed removal; the rebased main strengthened the bundled skill content (#160 cost-guardrail and registry-gating rails) and removing the surface would cost analyst documentation that the project actively maintains.
- `agnes auth token {create,list,revoke}` stays under `agnes auth` (where it lives today). No top-level `da token` group is added.
```
