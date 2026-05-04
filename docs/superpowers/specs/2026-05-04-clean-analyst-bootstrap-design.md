# Clean analyst bootstrap — design

**Date:** 2026-05-04
**Branch:** `zs/clean-analyst-bootstrap-spec`
**Status:** Draft (approved by user, pre-implementation)
**Successor to:** today's `da analyst setup` flow (interactive email/password) and the empty-folder bug under `da sync`.

## Problem

A new analyst should be able to:

1. Sign in to the Agnes web UI.
2. Click a button on `/install?role=analyst`, copy a single Claude-Code-paste prompt to the clipboard.
3. In an empty terminal, in an empty folder, paste the prompt into Claude Code.
4. Have Claude Code do **all** of the local setup — install the `da` CLI, trust the server's TLS cert (when needed), authenticate, generate `CLAUDE.md`, install Claude Code hooks, pull the RBAC-allowed parquets, build the local DuckDB views, write a human-readable workspace docs file.
5. Immediately start asking questions about the data — without ever typing a follow-up command.
6. From the second session onwards, have data freshness handled automatically by hooks (no `da sync` ever typed by hand).

Today this flow does not exist. The closest piece (`da analyst setup` in `cli/commands/analyst.py`) is interactive (prompts for email + password), produces a workspace layout that does not match what `da sync` later writes (the `data/parquet/`, `data/duckdb/`, `data/metadata/`, `user/artifacts/` directories it creates are never read by anything; `da sync` writes parquets to a sibling `server/parquet/` and DuckDB to `user/duckdb/analytics.duckdb`), and never registers the SessionStart/End hooks unless the analyst already managed to authenticate.

In addition, `da sync` itself creates empty directories (`.claude/rules/` is `mkdir`-ed before the bundle is fetched, so when the analyst has no rule grants the directory exists empty), and `da fetch` opens a DuckDB connection unconditionally, materializing an empty database file the first time the user runs `da fetch` before any `da sync`.

## Goals

- Single web → paste → done UX. Zero interactive prompts in the CLI bootstrap path.
- Workspace contains only files that something writes intentionally. No empty directories, no unread caches.
- Reader commands (`query`, `catalog`, `schema`, `describe`, `snapshot list`, `disk-info`, `status`, `diagnose`, `auth whoami`, `fetch --estimate`) survive a freshly-bootstrapped workspace with zero table grants and zero corporate-memory rules without crashing.
- CLI verbs are mnemonic and non-overlapping. No two commands mean almost the same thing.
- The clean-install behavior is testable: integration tests that boot a real FastAPI server fixture, run the bootstrap end-to-end against `tmp_path`, and assert the exact set of files created.

## Non-goals

- Migration of existing analyst workspaces. This is a greenfield rewrite — no `data/parquet/` deprecation aliases, no `da analyst setup` shim that calls the new code. Any analyst running today's setup gets a different workspace shape; their old files are dead but harmless. Documented in CHANGELOG `**BREAKING**`.
- Admin CLI rewrite. The `/install?role=admin` path keeps its current shape (TLS bootstrap, marketplace + plugins + skills + diagnose). Admin onboarding is unrelated.
- Offline initial bootstrap.
- PAT auto-refresh / refresh tokens.
- Multi-user / shared workspace.
- Web UI redesign. We add `?role=` query branching; no visual redesign.

## Architecture overview

```
┌──────────────────────────────┐
│  Web /install?role=analyst   │
│                              │   paste prompt
│  user logs in (web session)  ├────────────────────┐
│  clicks "Generate prompt"    │                    │
│  → POST /auth/tokens         │                    │
│    {scope:"bootstrap-analyst"│                    │
│     ttl:3600}                │                    │
│  → renders setup_instructions│                    │
│     .render(role="analyst")  │                    │
└──────────────────────────────┘                    │
                                                    ▼
                                       ┌──────────────────────────────┐
                                       │  Empty folder + Claude Code  │
                                       │                              │
                                       │  paste prompt; Claude runs:  │
                                       │   0. (TLS trust if needed)   │
                                       │   1. uv tool install da      │
                                       │   2. da init                 │
                                       │       --server-url URL       │
                                       │       --token PAT            │
                                       │   3. da catalog (smoke)      │
                                       └─────────────┬────────────────┘
                                                     │
                                                     ▼
                                ┌────────────────────────────────────────────┐
                                │ <cwd> — workspace, fully usable post-init  │
                                │ ├── CLAUDE.md          (Claude rails)      │
                                │ ├── AGNES_WORKSPACE.md (human docs)        │
                                │ ├── .claude/                               │
                                │ │   ├── settings.json (model, perms,       │
                                │ │   │   hooks: SessionStart→`da pull`,     │
                                │ │   │          SessionEnd  →`da push`)     │
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

next session: SessionStart hook → da pull (incremental MD5)
              SessionEnd   hook → da push (sessions + CLAUDE.local.md)
```

Single source of truth for data path: `da pull`. `da init` is a thin orchestrator that does auth + writes templates + installs hooks + calls `da pull` once. No data-path code lives in `init`.

Single source of truth for the install prompt: `app/web/setup_instructions.py`. New `role: Literal["analyst", "admin"]` parameter branches the step list. TLS trust block is the only piece shared between the two roles.

Layered config (greenfield decision: hybrid C from brainstorming): `<cwd>/.agnes/config.yaml` and `<cwd>/.agnes/token.json` override `~/.config/da/`. Default is global; per-workspace override unlocks multi-instance use cases without touching the simple case.

## New CLI surface

The CLI is rewritten with mnemonic, non-overlapping verbs. There are no backward-compat aliases. Today's `da analyst *`, `da sync`, `da sync --upload-only`, `da fetch` are removed.

```
da init                         one-time workspace bootstrap (--server-url, --token)
da pull                         refresh registered data (server → workspace)
da push                         upload sessions + notes (workspace → server)

da query "SELECT ..."           local DuckDB SQL
da query --remote "SELECT ..."  server-side BQ passthrough

da catalog                      tables I have access to (RBAC-filtered)
da schema <table>               columns + types
da describe <table>             sample rows

da snapshot create <table> --where "..." --as <name>
da snapshot list
da snapshot drop <name>

da auth login                   interactive login (browser flow)
da auth import-token <PAT>      non-interactive
da auth whoami
da auth logout

da token create
da token list
da token revoke <id>

da status                       what's in this workspace, when last synced
da diagnose                     health check (server + local)
```

Removed:
- `da analyst setup`, `da analyst status` — the `analyst` namespace had only one user role; the namespace is dead weight. Replaced by top-level `da init` and `da status`.
- `da sync` (and `--upload-only`) — split into `da pull` + `da push` for git-flavored mnemonics. Hook commands rename accordingly.
- `da fetch` — folded into `da snapshot create` so snapshots are a self-contained group (`create/list/drop`).
- `da metrics` as a separate namespace — folded into `da catalog --metrics` (low-traffic, doesn't deserve its own group).
- `da skills` — admin/dev tool, removed from the analyst CLI surface entirely.

## Components

### Server-side (Python, FastAPI)

| Component | File | Change |
|---|---|---|
| Analyst install-prompt branch | `app/web/setup_instructions.py` | Add `role: Literal["analyst","admin"]="admin"` to `resolve_lines()` and `render_setup_instructions()`. Analyst layout: TLS trust block (reused, when `ca_pem` supplied) → install `da` (reused) → `da init --server-url X --token Y --workspace .` → `da catalog` smoke verify → confirm. Drop for analyst: marketplace, plugins, skills, diagnose, login, whoami (all subsumed by `da init`). |
| `/install?role=...` query branching | `app/web/router.py:723` | Read `role` query param, default `"admin"`. Pass to `render_setup_instructions(role=...)`. |
| `install.html` UI | `app/web/templates/install.html` | Two role tiles: "Analyst workspace" / "Admin CLI". PAT mint button per tile, posts to `/auth/tokens` with `scope` matching the tile. Renders the prompt for the selected role. |
| PAT scope + TTL clamp | `app/api/tokens.py` | Add `scope` field to PAT-create request body (default `"general"`). For `scope="bootstrap-analyst"` the server force-clamps `ttl_seconds <= 3600` regardless of request. Audit-log entry includes the scope. |

### Client-side (CLI, Python)

| Component | File | Change |
|---|---|---|
| `da init` (new) | `cli/commands/init.py` (new) | Required args: `--server-url`, `--token`. Optional: `--force`, `--workspace` (default `cwd`). Steps: (1) verify server reachability + PAT validity via `/api/health` with `Authorization: Bearer <PAT>`; (2) save server URL + PAT to layered config (per-workspace if `<cwd>/.agnes/` exists, else global `~/.config/da/`); (3) write `CLAUDE.md` from server-side template; (4) write `.claude/settings.json` (model, permissions, hooks pointing at `da pull` and `da push`); (5) write `.claude/CLAUDE.local.md` (stub, only if absent); (6) call `da pull` programmatically (refactor below); (7) write `AGNES_WORKSPACE.md` from server-side template with timestamp + server URL substituted. |
| `da pull` (renamed from `da sync`) | `cli/commands/pull.py` (renamed from `cli/commands/sync.py`) | Behavior is today's `da sync` minus the `--upload-only` branch. Lazy-mkdir fixes (see below). Refactor `main()` so `da init` can call it programmatically without a Typer wrapper. |
| `da push` (extracted from `da sync --upload-only`) | `cli/commands/push.py` (new) | Uploads `user/sessions/*.jsonl` and `.claude/CLAUDE.local.md`. Lazy: skip when nothing to upload. Same auth as `da pull`. |
| `da snapshot create` (renamed from `da fetch`) | `cli/commands/snapshot.py` | Move logic from `cli/commands/fetch.py` into a `create` subcommand of the existing `snapshot` group. Remove `cli/commands/fetch.py`. Add existence check before opening DuckDB to avoid creating an empty DB file when no `da pull` has run yet (guard: `if not db_path.exists(): typer.echo("Local DuckDB not found. Run: da pull"); raise typer.Exit(1)`). |
| `da status` (renamed from `da analyst status`) | `cli/commands/status.py` (renamed from analyst.py status fn) | Path refs updated to new layout: `server/parquet/`, `user/duckdb/analytics.duckdb`. Drop `data/metadata/last_sync.json`; use mtime on `user/duckdb/analytics.duckdb` as freshness proxy. |
| Layered config loader | `cli/config.py` | `load_config()`/`load_token()` first check `<cwd>/.agnes/{config.yaml,token.json}`; fall back to `~/.config/da/`. `save_*()` writes per-workspace if a per-workspace file exists, else global. `DA_LOCAL_DIR` env var continues to override `cwd` for testing. |
| Lazy-mkdir contract | `cli/commands/pull.py`, all writers | No `mkdir(parents=True, exist_ok=True)` before a conditional write loop. Mkdir only immediately before the first file write. Concretely: `_fetch_and_write_rules` mkdirs `.claude/rules/` only when `mandatory ∪ approved` is non-empty; `parquet_dir` mkdir is inlined into the per-table download loop. |
| `da catalog --metrics` flag | `cli/commands/catalog.py` | Add `--metrics` flag that switches output to the metric definitions list (formerly `da metrics list`). Other `da metrics *` subcommands removed. |
| Removed: `da metrics`, `da skills`, `da fetch`, `da analyst *`, `da sync` | `cli/commands/{metrics.py,skills.py,fetch.py,analyst.py,sync.py}` | Deleted (greenfield). |

### Templates

| Component | File | Change |
|---|---|---|
| `CLAUDE.md` template | `config/claude_md_template.txt` | Update path references: `data/parquet/` → `server/parquet/`, `data/duckdb/...` → `user/duckdb/analytics.duckdb`. Replace `da sync` → `da pull`, `da fetch` → `da snapshot create`. Add a one-line pointer: "For human docs, see `AGNES_WORKSPACE.md` in this folder." |
| `AGNES_WORKSPACE.md` template (new) | `config/agnes_workspace_template.txt` (new) | Static template with three placeholders: `{created_at}`, `{server_url}`, `{workspace_path}`. Content described in dedicated section below. |

## Web UI flow

```
GET /install?role=analyst
   └─ render install.html with `role=analyst` context
      ├─ "Analyst workspace" tile is active (visual highlight)
      ├─ "Admin CLI" tile linked to /install?role=admin

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

PAT lifecycle: minted on click, single TTL window (1h), no auto-revoke after first use. The user has 1h to retry the bootstrap if a step fails. After 1h they re-click "Generate prompt" for a new PAT. The bootstrap PAT is functionally indistinguishable from a regular PAT once minted; the `scope` claim is informational (audit trail), not enforced per-endpoint. Per-endpoint scope enforcement is a follow-up issue.

## Workspace layout

Post-`da init`, workspace contains exactly:

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

- `./.claude/rules/km_*.md` — only when `/api/memory/bundle` returns ≥1 mandatory or approved item.
- `./server/parquet/<table>.parquet` — only when `/api/sync/manifest` returns ≥1 table the user has grants on.
- `./user/snapshots/<name>.parquet` — only after the user runs `da snapshot create <table> --as <name>`.
- `./user/sessions/<id>.jsonl` — only after the SessionEnd hook runs `da push` against captured Claude Code sessions.
- `./.agnes/config.yaml`, `./.agnes/token.json` — only if the user opts into per-workspace config (advanced; not created by default).

Forbidden under any circumstances (these are the dead paths today's setup creates):

- `./data/parquet/`, `./data/duckdb/`, `./data/metadata/`, `./user/artifacts/` — none of these were read by any code path; removed entirely.

## Data flow / sequence

```
Empty folder + Claude Code with paste prompt
│
├─ Step 0 (TLS trust block) — only when server uses private CA
│   writes ~/.agnes/{ca.pem, ca-bundle.pem}, appends shell rc block
│
├─ Step 1 — uv tool install da
│   writes ~/.local/bin/da
│
├─ Step 2 — da init --server-url URL --token PAT --workspace .
│   ├─ verify: GET /api/health with Bearer PAT  → 200
│   ├─ save: ~/.config/da/{config.yaml, token.json}     (or per-workspace if <cwd>/.agnes/ exists)
│   ├─ write: ./CLAUDE.md
│   ├─ write: ./.claude/settings.json (with hooks SessionStart→`da pull`, SessionEnd→`da push`)
│   ├─ write: ./.claude/CLAUDE.local.md (stub, if absent)
│   ├─ call:  da pull (programmatic)
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
│   └─ write: ./AGNES_WORKSPACE.md
│
├─ Step 3 — da catalog (smoke verify)
│   confirms end-to-end works; prints table count
│
└─ Step 4 — confirm
    Claude reports: tables synced, files created, hooks active.

Subsequent sessions:
├─ SessionStart hook fires: da pull --quiet 2>/dev/null || true
├─ user works
└─ SessionEnd hook fires:   da push --quiet 2>/dev/null || true
```

## Empty-folder discipline

The clean-install contract has two halves: writers must be lazy (don't pre-allocate empty dirs); readers must be tolerant (don't crash on missing dirs).

### Writer contract

> Every writer MUST mkdir its target *immediately before* the first file write, never as bulk pre-allocation. If 0 files are written, no mkdir happens.

Concretely:

| Writer | File:line | Today | After |
|---|---|---|---|
| `_fetch_and_write_rules` | `cli/commands/sync.py:222` | `rules_dir.mkdir(parents=True, exist_ok=True)` before iterating | Check `mandatory + approved` first; if empty, return without mkdir. |
| Per-table download loop | `cli/commands/sync.py:120, 529` | `parquet_dir.mkdir(parents=True, exist_ok=True)` before loop | Mkdir inlined into the per-file write block; first table triggers mkdir. |
| `_install_claude_hooks` | `cli/commands/analyst.py:290` | mkdir `.claude/` | unchanged — `.claude/` always has content (settings.json is load-bearing). |
| `_rebuild_duckdb_views` | `cli/commands/sync.py:321` | mkdir `user/duckdb/` | unchanged — DuckDB file is opened unconditionally as part of view rebuild; the file is the load-bearing artifact, not just the directory. |
| `da push` upload | (new) `cli/commands/push.py` | (n/a) | Mkdir `user/sessions/` only inside the per-session-write branch; `da push` with nothing to upload exits 0 without touching disk. |
| `da snapshot create` parquet write | `cli/commands/snapshot.py` | mkdir `user/snapshots/` before write | unchanged (snapshot create is the canonical writer; mkdir on first write is correct). |

### Reader contract

> Every reader MUST handle missing paths gracefully. "Gracefully" means:
> - **Exit 0 with empty / zero output** when missing paths are a natural empty answer (`da disk-info` shows 0; `da status` shows "initialized: no").
> - **Exit 1 with friendly hint** when the missing path means a workflow precondition isn't met (`da query`: "Local DuckDB not found. Run: da pull").
> - **Never create the path side-effect-ally** unless this command is the canonical writer for it.

Audit of current readers (only commands that touch the filesystem are listed; others are server-API only and unaffected):

| Command | Path it reads | Today's behavior | Change needed |
|---|---|---|---|
| `da query` | `user/duckdb/analytics.duckdb` | `.exists()` check, friendly "Run: da sync" exit 1 | Update hint text → "Run: da pull". |
| `da explore` | same | `.exists()` check, friendly exit | Update hint text. |
| `da snapshot create` (was `da fetch`) | same | unconditional `duckdb.connect()` → creates empty DB | Add `.exists()` check + hint "Run: da pull first". |
| `da snapshot create` (write side) | `user/snapshots/` | unchanged (writer, mkdir at first write) | unchanged. |
| `da disk-info` | `user/snapshots/` | `.exists()` guards around sum/count/free | unchanged. |
| `da snapshot list` | `user/snapshots/` | glob safe on missing | unchanged (glob returns empty iterator on missing dir). |
| `da push` | `user/sessions/` | `.exists()` check before iterating | unchanged. |
| `da status` | `server/parquet/`, `user/duckdb/...` | path strings reference legacy `data/parquet/` etc. | Update path strings; `.exists()` checks already in place. |

### Regression guard (test)

```python
def assert_no_dead_dirs(workspace: Path):
    """Workspace must not contain pre-allocated empty directories."""
    forbidden_unconditional = ["data/parquet", "data/duckdb", "data/metadata",
                               "user/artifacts"]
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

Generated by `da init` in the workspace root. Not state — pure documentation. Idempotent overwrite on every `da init` (preserves nothing, regenerates everything from the static template).

Three placeholders only: `{created_at}`, `{server_url}`, `{workspace_path}`. No email, no user identity, no role. Email is not used anywhere in the analyst CLI flow; PAT identifies the user server-side, and decoded JWT email is informational at best — we drop it from this header for clarity.

Sections:

1. **Header** — `Created: <ISO timestamp>`, `Server: <URL>`, `Workspace: <abs path>`.
2. **What's installed (global, per-user)** — table of paths in `~/.local/bin/`, `~/.config/da/`, `~/.agnes/`, shell rc block. Each row: `path | what it is | how to remove`.
3. **What's in this folder** — table of paths in workspace. Each row: `path | what it is`. Notes which dirs are conditional ("only when grants/sessions/etc. exist").
4. **How it stays fresh** — explains SessionStart/End hooks: what they run, when, what failure looks like (silent, `|| true`).
5. **Cheat sheet** — `da pull`, `da catalog`, `da query`, `da snapshot create`, `da status`, `da init --force` examples.
6. **Uninstall** — step-by-step recipe to remove the CLI globally, the config dir, the trust artifacts, the rc block, and the workspace itself.

Approximate size: 3.5 KB, ~100 lines. Disk overhead: nil.

The content is written at the human, not at Claude. `CLAUDE.md` is for the AI; `AGNES_WORKSPACE.md` is for the person reading `ls`.

PAT value never appears in `AGNES_WORKSPACE.md` — only its location (`~/.config/da/token.json`). The token file is `chmod 600`.

## Error handling

| Failure | Detection | Behavior |
|---|---|---|
| Server unreachable during `da init` | `httpx.ConnectError` on `/api/health` | exit 1, hint: "Cannot reach `<URL>` — check network or server status". |
| PAT expired | `/api/health` → 401 | exit 1, hint: "Token expired — get a fresh one at `<URL>/install?role=analyst`". |
| PAT invalid (mis-paste) | 401, JWT decode failure | exit 1, hint: "Token format invalid — re-copy from `/install`". |
| TLS trust failure | curl/wheel install fails with `unknown CA` | exit 1, hint refers user back to paste-prompt step 0. |
| Disk full during `da pull` | `OSError(ENOSPC)` on parquet write | atomic rename → partial file deleted; exit 1 with disk-info dump. |
| Concurrent `da init` in same folder | file lock on `<cwd>/.claude/settings.json` (or sentinel `<cwd>/.agnes/.init.lock`) | second invocation: "Setup already running" exit 1. |
| Partial state (previous `da init` crashed mid-way) | `CLAUDE.md` exists but `.claude/settings.json` missing | `da init` (without `--force`): friendly hint "Workspace partially set up — run `da init --force` to redo". |
| `da pull` 401 mid-session (PAT revoked server-side) | response 401 from `/api/sync/manifest` | hook command prints warning, exits 0 (`|| true`); session continues with last-known data. Manual `da pull` next time prints actionable hint. |
| Empty manifest | `/api/sync/manifest` → `{"tables": []}` | success, no parquet dir created, no warning (valid state). |
| Empty memory bundle | `/api/memory/bundle` → `{"mandatory": [], "approved": []}` | success, no `.claude/rules/` dir (valid state). |
| Per-table 5xx mid-pull | per-table 500 from `/api/data/<id>/download` | per-table warn; pull continues; final exit 0 if at least one table succeeded, exit 1 if all failed. |
| Workspace path with spaces / unicode | path passed to subprocess as `cwd=`, no shell interpolation | works as-is; tested in clean-install integration test. |
| Hook fires in unrelated Claude Code session | settings.json is workspace-scoped (`<cwd>/.claude/settings.json`) | hook does not fire; Claude Code reads settings only for the directory it was opened in. |

Principle: hooks always end `|| true` so they never block a session. Manual commands are exit-1-with-hint. No silent failures in interactive flow.

## Verification / testing

Verification has three layers: (a) automated reader-smoke matrix that proves no command crashes on a freshly-bootstrapped workspace; (b) automated clean-install integration tests that prove the workspace contains exactly the expected files; (c) a manual end-to-end protocol that runs the actual paste prompt against a real local server.

### 5.1 Reader smoke matrix (automated)

```python
@pytest.mark.parametrize("cmd", [
    ["da", "catalog"],
    ["da", "schema", "any_table"],
    ["da", "describe", "any_table"],
    ["da", "query", "SELECT 1"],
    ["da", "explore", "any_view"],
    ["da", "disk-info"],
    ["da", "snapshot", "list"],
    ["da", "status"],
    ["da", "snapshot", "create", "any_table", "--estimate"],
    ["da", "diagnose"],
    ["da", "auth", "whoami"],
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
    """User has 3 table grants, 2 mandatory rules → expected workspace shape."""
    subprocess.run(
        ["da", "init", "--server-url", fastapi_test_server.url,
         "--token", test_pat, "--workspace", str(tmp_path)],
        check=True,
    )
    # Required:
    for must in ["CLAUDE.md", "AGNES_WORKSPACE.md",
                 ".claude/settings.json", ".claude/CLAUDE.local.md",
                 "user/duckdb/analytics.duckdb"]:
        assert (tmp_path / must).exists(), f"missing required: {must}"
    # Conditional (present because grants/rules exist):
    assert len(list((tmp_path / "server" / "parquet").glob("*.parquet"))) == 3
    assert len(list((tmp_path / ".claude" / "rules").iterdir())) == 2
    # Forbidden:
    assert_no_dead_dirs(tmp_path)
    # Hooks installed correctly:
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert any("da pull" in h["hooks"][0]["command"]
               for h in settings["hooks"]["SessionStart"])
    assert any("da push" in h["hooks"][0]["command"]
               for h in settings["hooks"]["SessionEnd"])


def test_clean_install_zero_grants(fastapi_test_server, tmp_path, test_pat_no_grants):
    """User has 0 grants, 0 rules → minimal workspace, zero dead dirs."""
    subprocess.run(["da", "init", ...], check=True)
    must_exist = {"CLAUDE.md", "AGNES_WORKSPACE.md",
                  ".claude/settings.json", ".claude/CLAUDE.local.md",
                  "user/duckdb/analytics.duckdb"}
    must_not_exist = {".claude/rules", "server/parquet", "data/parquet",
                      "data/duckdb", "data/metadata", "user/artifacts",
                      "user/sessions", "user/snapshots"}
    for p in must_exist:
        assert (tmp_path / p).exists()
    for p in must_not_exist:
        assert not (tmp_path / p).exists()
    assert_no_dead_dirs(tmp_path)


def test_setup_force_preserves_user_files(...):
    """`da init --force` regenerates CLAUDE.md and AGNES_WORKSPACE.md
    but never touches CLAUDE.local.md."""

def test_readers_in_pre_setup_dir(tmp_path, test_pat):
    """User runs reader commands in a folder that never had `da init`.
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
    assert "da init" in text
    assert "--token" in text and "agnes_pat_TEST" in text
    assert "--server-url" in text
    assert "da catalog" in text
    # Must not contain (admin-only):
    assert "marketplace" not in text
    assert "claude plugin install" not in text
    assert "da skills" not in text
    assert "da diagnose" not in text
```

### 5.4 PAT scope/TTL test

```python
def test_bootstrap_pat_ttl_clamped_to_one_hour(client, web_session):
    resp = client.post("/auth/tokens", json={
        "scope": "bootstrap-analyst",
        "ttl_seconds": 86400,  # ignored — server force-clamps
        "name": "init",
    })
    assert resp.status_code == 200
    pat = resp.json()["pat"]
    payload = jwt.decode(pat, options={"verify_signature": False})
    assert payload["scope"] == "bootstrap-analyst"
    assert payload["exp"] - payload["iat"] <= 3600 + 5
```

### 5.5 Manual clean-install protocol (pre-merge)

1. `git clean -fdx` in the repo (no build artifacts).
2. Boot FastAPI locally against a clean test instance state.
3. Empty terminal in `/tmp/test-analyst-1`. From the web `/install?role=analyst`, paste prompt.
4. `tree -a /tmp/test-analyst-1` and compare with the expected tree from §5.2.
5. `claude` in that folder. Three queries: "what tables can I see", "SELECT count(*) FROM <t>", "show me last 5 rows of <t>". All must work without further intervention.
6. `/exit`. Verify SessionEnd hook ran (server-side audit log shows `da push`; `du -sh /tmp/test-analyst-1/user/sessions/` non-empty).
7. Second `claude` in same folder. Verify SessionStart hook fires (`da pull` request in audit log).
8. Second workspace `/tmp/test-analyst-2` with the same PAT (within TTL). Repeat 3-5. Verify global `~/.config/da/` is not duplicated; the second workspace has its own DuckDB.

This protocol is documented in `docs/RELEASE_CHECKLIST.md` as a mandatory pre-merge step for changes touching the bootstrap path.

## Out of scope

1. **Admin CLI tooling** — `/install?role=admin` and `da admin *` continue unchanged.
2. **Migration of existing analyst workspaces** — greenfield; old `data/parquet/` etc. are dead but harmless.
3. **Backward-compat aliases** — no `da analyst setup` → `da init` shim, no `da sync` → `da pull` shim. Hard cutover.
4. **Multi-user / shared workspace** — `<cwd>` is single-user.
5. **Offline initial bootstrap** — `da init` requires server reachability.
6. **PAT auto-refresh / refresh tokens** — bootstrap PAT expires after 1h; user re-clicks "Generate prompt".
7. **Per-endpoint PAT scope enforcement** — `bootstrap-analyst` scope is informational at this stage (audit-trail). Per-endpoint enforcement is a follow-up issue.
8. **Web UI redesign** — `/install?role=...` reuses the existing page shell + JS. No visual redesign.
9. **CLI rename adjacent commands** beyond what's listed (e.g., `da auth login` → `da login`) — out of scope.

## Open questions / follow-ups

- **Per-endpoint PAT scope enforcement** — should `scope="bootstrap-analyst"` PATs be restricted to `/api/health`, `/api/sync/manifest`, `/api/data/*/download`, `/api/memory/bundle` only, and refused on (e.g.) `/api/admin/*`? Today not enforced. New issue.
- **`da catalog --metrics`** — folding `da metrics` into a flag may be too aggressive; metrics has its own `show <id>`, `import` subcommands today. Open question whether to fully delete or just demote to `da catalog --metrics list` and keep `da catalog --metrics show <id>` etc. To be resolved in implementation plan.
- **`da snapshot create` UX** — does the `--where` clause keep BigQuery SQL flavor or switch to DuckDB flavor? Today `da fetch` uses BQ flavor. Keep BQ flavor for parity with `da query --remote`.
- **Hook performance budget** — `da pull` on a 1.1 GB workspace (real-world example: today's `tmp_oss/server/parquet/`) with all parquets unchanged should complete the manifest comparison in well under 1 s so SessionStart doesn't perceptibly delay the user. If incremental MD5 comparison is too slow at scale, consider a server-side ETag.
- **Anti-coupling test** — add a test that imports every `cli/commands/*.py` module and asserts no module imports any other `cli.commands.*` module except via dispatch (Typer subcommand registration). Prevents the `init` command from accidentally re-importing `pull` internals in a way that creates hidden coupling.

## CHANGELOG entry (preview)

```markdown
## [Unreleased]

### Changed
- **BREAKING** Analyst bootstrap rewritten end-to-end. `da analyst setup` is removed; replaced by `da init` (non-interactive, requires `--server-url` and `--token`). `da sync` is split into `da pull` (refresh) and `da push` (upload). `da fetch` is folded into `da snapshot create`. `da metrics` is folded into `da catalog --metrics`. `da skills` is removed from the analyst CLI. The `da analyst` namespace is removed; the workspace status command is now `da status`.
- **BREAKING** Workspace layout simplified. Removed: `data/parquet/`, `data/duckdb/`, `data/metadata/`, `user/artifacts/`. Canonical paths: `server/parquet/` (synced parquets), `user/duckdb/analytics.duckdb` (DuckDB views), `user/snapshots/` (ad-hoc snapshots), `user/sessions/` (recorded sessions).
- The `/install` web page now branches on a `role` query parameter: `/install?role=analyst` renders the analyst workspace bootstrap prompt; `/install?role=admin` renders the admin CLI install prompt.

### Added
- `AGNES_WORKSPACE.md` — human-readable workspace docs file generated by `da init` in the workspace root. Documents global install, workspace layout, hooks, cheat sheet, uninstall recipe.
- Layered config: `<cwd>/.agnes/{config.yaml,token.json}` overrides `~/.config/da/`, enabling multi-instance use cases.
- PAT scope field. PATs minted with `scope="bootstrap-analyst"` are TTL-clamped to ≤ 1 h server-side.

### Fixed
- `da pull` (formerly `da sync`) no longer creates `.claude/rules/` when the corporate-memory bundle is empty.
- `da pull` no longer creates `server/parquet/` when the manifest is empty.
- `da snapshot create` (formerly `da fetch`) no longer materializes an empty `user/duckdb/analytics.duckdb` when run before any `da pull`.
- Workspace `da status` reads from the canonical `server/parquet/` and `user/duckdb/analytics.duckdb` paths (was reading legacy `data/parquet/`, `data/metadata/last_sync.json`).

### Removed
- `da analyst setup`, `da analyst status`, `da sync`, `da fetch`, `da metrics`, `da skills`. See "Changed" above for replacements.
- Legacy workspace directories `data/parquet/`, `data/duckdb/`, `data/metadata/`, `user/artifacts/`. Existing analyst workspaces should be reinitialized with `da init --server-url ... --token ... --force` (a fresh empty folder is recommended).
```
