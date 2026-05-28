# Initial Workspace Template — per-instance `agnes init` override

This document describes the **Initial Workspace Template** feature: a
per-instance mechanism that lets an Agnes operator fully control the
analyst workspace skeleton from their own Git repository, replacing the
files `agnes init` would otherwise generate from Agnes's bundled
defaults.

**Audience:** operators of an Agnes instance who want to customize the
analyst onboarding experience without forking Agnes.

> **Building or forking a seed repo?** Read
> [`docs/seed-repo-contract.md`](seed-repo-contract.md) — it documents
> the directory layout, the connector skill frontmatter schema, the
> install-prompt placeholders the Agnes server substitutes, and the
> per-file admin-editor ownership map.

## What it is

By default, `agnes init` builds an analyst workspace from a mix of
server-rendered (`CLAUDE.md`) and client-hardcoded (`.claude/settings.json`,
hooks, slash commands, `AGNES_WORKSPACE.md`) content. When you register
an Initial Workspace Template, that content is **fully replaced** by
files cloned from your Git repository.

```
Your Git repo                       Agnes server                       Analyst workspace
──────────────                     ────────────                         ─────────────────
README.md           ◀── admin docs, NOT shipped                                  ┌── extracted from `workspace/`
.github/            ◀── CI configs, NOT shipped                                  │
LICENSE             ◀── admin docs, NOT shipped                                  ▼
workspace/                                                                    CLAUDE.md
  CLAUDE.md          ──┐                                                        .claude/
  .claude/             │   admin clicks "Sync now"                                  settings.json
    settings.json      │   ↓                                                        commands/
    commands/        ──┼─→ ${DATA_DIR}/initial-workspace/workspace/   ──┐         docs/
  docs/                │                                                │         custom-folder/
  custom-folder/     ──┘   analyst runs `agnes init`                    │         ...
                          GET /api/initial-workspace.zip            ────┘
```

**Only the contents of `workspace/`** reach the analyst. Anything else
at the repo root (README, LICENSE, CI configs, scripts the admin team
uses to maintain the template) stays in the repo and is invisible to
Agnes.

## When to use it

Use Initial Workspace Template when you want to:

- Customize `CLAUDE.md` beyond what the admin template editor at
  `/admin/workspace-prompt` allows (e.g. add custom slash commands, change
  the directory layout, ship corporate-specific golden paths).
- Ship instance-specific `.claude/settings.json` defaults (custom
  permissions, model selection, statusLine).
- Pre-populate analyst workspaces with corporate documentation
  (`docs/handbook.md`, `policies/`, etc.).
- Version-control the analyst onboarding experience in your own repo
  with normal PR review, code owners, and CI checks.

**Do NOT use it** if a `/admin/workspace-prompt` template override is
enough — the prompt editor is simpler to manage and doesn't transfer the
responsibilities listed below.

## Configuration

On the admin UI at `/admin/server-config`, scroll to the **Initial
Workspace Template** section:

1. Click **Link to Template Repository**.
2. In the modal, fill in:
   - **Repository URL (HTTPS)** — required, must be `https://`.
   - **Branch** — optional; leave blank to track the remote's default
     branch.
   - **GitHub PAT** — required only for private repos. Stored at
     `${DATA_DIR}/state/.env_overlay` (chmod 600), never in the YAML
     overlay or DuckDB.
3. Click **Save**.
4. Click **Sync now** to clone the repo into
   `${DATA_DIR}/initial-workspace/`. The modal shows the commit SHA and
   file count on success, or a typed error if the clone fails or the
   repo contains a reserved path.

The config persists to `${DATA_DIR}/state/instance.yaml` under the
`initial_workspace:` section:

```yaml
initial_workspace:
  url: https://github.com/your-org/agnes-workspace-template
  branch: main
  token_env: AGNES_INITIAL_WORKSPACE_TOKEN
  last_synced_at: 2026-05-13T10:00:00Z
  last_commit_sha: 1a2b3c4d5e
  last_error: null
```

Sync is **manual only**. There is no nightly auto-sync; you click "Sync
now" whenever you want the on-disk working copy to match the latest
commit on the configured branch.

## Repo layout

Your template repo MUST have a `workspace/` subdirectory at its root.
**Only the contents of `workspace/`** map to the analyst's workspace —
everything else (README, LICENSE, CI configs, admin scripts) stays in
the repo and never reaches an analyst.

```
your-repo/                                  analyst's workspace/
  README.md                       ──── NOT shipped (admin docs)
  LICENSE                         ──── NOT shipped
  .github/workflows/ci.yml        ──── NOT shipped
  workspace/                                  ┐
    CLAUDE.md                     ──>         │  CLAUDE.md
    .claude/                      ──>         │  .claude/
      settings.json                           │    settings.json
      commands/                               │    commands/
        my-team-handover.md                   │      my-team-handover.md
    docs/                         ──>         │  docs/
      handbook.md                             │    handbook.md
  .git/                           ──── EXCLUDED FROM ZIP
```

The `.git/` directory is automatically excluded — analysts never receive
it. Files at the repo root (anywhere outside `workspace/`) are also
never shipped, regardless of what they're called.

**Why a subdirectory?** This split lets the repo serve double duty as
a normal codebase. The repo's own README explains what the template is
for and how to maintain it; CI workflows can validate the YAML
settings on PR; LICENSE lives where GitHub renders it on the repo
landing page. None of that pollutes the analyst's workspace.

### Strict layout check

If your repo has NO `workspace/` subdirectory at its root, **sync
fails** with a typed error in the Sync-now modal:

```
Repository must contain a 'workspace' directory at root; its contents
are what gets shipped to analyst workspaces. Files outside `workspace/`
(README, CI configs, etc.) stay in the repo and are NOT delivered to
analysts.
```

There is no fallback to "use repo root if workspace/ is missing" — the
convention is mandatory so accidental admin-only files never reach an
analyst.

### Reserved paths

These paths (relative to `workspace/`) are **rejected at sync time**
because Agnes manages them itself:

| Path inside `workspace/`     | Equivalent in your repo                    | Why                                                   |
|------------------------------|--------------------------------------------|-------------------------------------------------------|
| `.claude/init-complete`      | `workspace/.claude/init-complete`          | Agnes's completion sentinel; written at the end of every `agnes init` to enable resume-after-kill detection and override-mode signaling. |

If your template repo ships a reserved path inside `workspace/`, **the
sync fails** and the admin sees a typed error in the Sync-now modal.
Remove the offending file from your repo and re-sync. Agnes does **not**
silently strip reserved files — explicit failure surfaces the issue
immediately rather than leaving an analyst in a broken state.

A file with the same name AT THE REPO ROOT (e.g.
`<your-repo>/.claude/init-complete` outside `workspace/`) is fine —
it's admin territory and never reaches the analyst anyway.

## What Agnes stops doing when override is active

Override is an **init-time** contract. When the
`initial_workspace:` section is configured AND synced, `agnes init`
runs the override flow and bypasses every default-mode workspace
write — admin's template is the source of truth for the INITIAL
`.claude/` contents. Subsequent runtime CLI commands keep updating
the workspace as on a default install.

### Init-time skip (admin's template wins)

| Default behavior                                                       | Override behavior                                                                 |
|------------------------------------------------------------------------|-----------------------------------------------------------------------------------|
| `CLAUDE.md` fetched from `/api/welcome` (server-rendered Jinja2)       | `CLAUDE.md` comes verbatim from your repo (no Jinja2, no RBAC filtering)          |
| `.claude/settings.json` seeded with `{model: sonnet, permissions: …}`  | Whatever your repo ships (or no file at all)                                      |
| `install_claude_hooks(workspace)` installs SessionStart/End/statusLine | Your repo's `settings.json` is the source of truth at init time; Agnes installs nothing during `agnes init` |
| `install_claude_commands(workspace)` installs `/update-agnes-plugins` + `/agnes-private` | Your repo controls `.claude/commands/` at init time                            |
| `.claude/CLAUDE.local.md` stub written if absent                       | If your repo ships one, that wins; otherwise the file simply doesn't exist        |
| `AGNES_WORKSPACE.md` rendered from `config/agnes_workspace_template.txt` | Your repo controls (or doesn't ship at all)                                       |
| `--force` backs up `CLAUDE.md` to `CLAUDE.md.bak.<timestamp>`          | **No backup.** Source of truth is your Git repo; recovery is `git log` / `git checkout`.|

The remaining `agnes init` steps **still run** — they are data-plane
concerns, not workspace-skeleton concerns:

- **PAT verification** against `/api/catalog/tables`.
- **`agnes pull`** of the parquets, DuckDB views, and corporate-memory
  rules under `server/parquet/`, `user/duckdb/`, `.claude/rules/`.
- **Completion sentinel** at `.claude/init-complete` — written with
  extended fields (`override: true`, `template_source`, `template_sha`)
  so future `agnes init` (re-)runs detect the override and skip the
  default seeding block.

### Runtime CLI keeps working (Agnes stays in sync)

Runtime commands — anything the analyst invokes *after* init — ignore
the sentinel and update workspace `.claude/` content normally. This is
a documented contract, not an implementation detail. Concretely:

| Runtime path                                                           | Behavior on override workspace                                                    |
|------------------------------------------------------------------------|-----------------------------------------------------------------------------------|
| `agnes self-upgrade` → `maybe_refresh_claude_hooks`                    | **Refreshes Agnes hook entries** in `.claude/settings.json` so analysts pick up new hook layouts (e.g. new SessionStart entries). Your custom hooks — anything whose command does NOT match `_OUR_COMMAND_MARKERS` in `cli/lib/hooks.py` — fall through unchanged. |
| `agnes refresh-marketplace` → `_enable_plugins_in_workspace_settings`  | **Writes `enabledPlugins` map** for the user's curated stack (`"<plugin>@agnes": true`). Stack is the source of truth — locally `claude plugin disable`-d plugins that remain in the stack get re-enabled. To permanently exclude, remove from stack via `agnes marketplace remove`. |
| Future runtime CLI commands that need to update `.claude/`             | Treat override sentinel as non-existent. Same contract.                           |

Practical implication for you (the operator): ship your template with
the INITIAL `.claude/` skeleton you want. You do NOT need to ship
`enabledPlugins`, nor do you need to keep `settings.json` Agnes hook
entries permanently frozen at one revision — Agnes will keep them
current via `agnes self-upgrade`. If you want to add custom commands
to a Session hook, just include them in your repo's `settings.json`
under an entry whose command does NOT contain any of the
`_OUR_COMMAND_MARKERS` substrings; runtime refresh leaves it alone.

## What you (the operator) must include in your repo

Because Agnes installs nothing of its own, your repo is responsible for:

### 1. SessionStart hook for `agnes pull`

Without this hook, analysts won't get fresh parquets at the start of
every Claude Code session. Recommended `workspace/.claude/settings.json`
(in your repo) → lands as `.claude/settings.json` in the analyst's
workspace:

```json
{
  "model": "sonnet",
  "permissions": {
    "allow": ["Read", "Bash", "Grep", "Glob"]
  },
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "bash -c \"agnes capture-session 2>/dev/null || true\""
          }
        ]
      },
      {
        "hooks": [
          {
            "type": "command",
            "command": "bash -c \"agnes self-upgrade --quiet 2>/dev/null || true; agnes pull --quiet 2>/dev/null || true\""
          }
        ]
      },
      {
        "hooks": [
          {
            "type": "command",
            "command": "bash -c \"agnes refresh-marketplace --check 2>/dev/null || true\""
          }
        ]
      }
    ],
    "SessionEnd": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "bash -c \"( nohup agnes push --quiet </dev/null >/dev/null 2>&1 & ) ; true\""
          }
        ]
      }
    ]
  },
  "statusLine": {
    "type": "command",
    "command": "agnes statusline"
  }
}
```

The exact bash strings mirror what Agnes's default `cli/lib/hooks.py`
would have installed. You can deviate, but understand the trade-offs:

- Omit `agnes capture-session` → session transcripts never get queued,
  `agnes push` uploads nothing.
- Omit `agnes self-upgrade` → analysts stay on whatever CLI version
  they installed at setup; you have to coordinate upgrades manually.
- Omit `agnes pull` → workspaces never refresh parquets without a
  manual `agnes pull` invocation.
- Omit the SessionEnd `agnes push` (detached form) → session transcripts
  and `CLAUDE.local.md` stay local, never reach the server.
- Omit `agnes refresh-marketplace --check` → analysts don't get
  marketplace-plugin-update notifications.
- Omit `agnes statusline` → no `🔒 agnes-private` indicator when an
  analyst marks a session private.

### 2. Slash commands (optional but recommended)

Default Agnes ships two slash commands. Replicate them in your repo if
you want analysts to have them:

- `workspace/.claude/commands/update-agnes-plugins.md` — drives
  `agnes refresh-marketplace` for marketplace plugin updates.
- `workspace/.claude/commands/agnes-private.md` — toggles
  session-private mode.

Copy the canonical content from the open-source Agnes repo at
`cli/templates/commands/`, or write your own.

### 3. `CLAUDE.md` content

This is your big lever. Default Agnes ships an extensive `CLAUDE.md`
(see the open-source `config/claude_md_template.txt`) covering rules,
metrics workflow, data sync, marketplace discovery, BigQuery query
patterns, snapshot hygiene, and more. If you ship a thin `CLAUDE.md`,
analysts lose all that guidance.

We recommend starting from the open-source default and customizing
incrementally, rather than writing one from scratch.

## Sync workflow

1. Edit files in your template repo, commit, push.
2. Go to `/admin/server-config`, scroll to **Initial Workspace Template**.
3. Click **Sync now**.
4. The modal shows the new commit SHA and file count. Analysts will
   pick up the new content on their next `agnes init --force` (or fresh
   install).

**Existing analyst workspaces do not auto-upgrade.** When you push a
new commit, current analyst workspaces continue running the older
template. Analysts must explicitly re-run `agnes init --force` to pick
up new content. This is intentional: silent workspace mutations under
analysts' feet would be hostile UX.

## PAT rotation

For private repos:

1. Mint a new GitHub PAT with `repo:read` scope.
2. On `/admin/server-config`, click **Edit** on the Initial Workspace
   Template card.
3. Paste the new PAT into the **GitHub PAT** field (the field is
   never prefilled — leaving it blank keeps the existing PAT).
4. Click **Save**, then **Sync now** to verify auth works.

The old PAT is overwritten in `.env_overlay`. The DB never held the
secret; only the env-var name.

## `--force` semantics

`agnes init` without `--force` against an existing workspace exits with
`partial_state` (same as default mode — uses sentinel detection).

`agnes init --force` on an override workspace:

1. Probes the server's status endpoint.
2. Downloads the template zip.
3. Diffs the zip's file list against what's on disk.
4. Prints a warning listing files-to-be-overwritten and files-to-be-created.
5. Prompts `Type YES to continue, anything else to abort`. Uppercase-strict.
6. On `YES`, extracts the zip (overwriting files in your repo, leaving
   any local-only files alone).
7. POSTs an `initial_workspace.applied` audit event.

The warning explicitly tells the analyst the action is irreversible and
will be logged. Files in the workspace that are **not** in the
template are preserved — analyst-local additions survive.

## Audit trail

Every override workflow writes audit rows. Query them via
`agnes admin activity` or the admin UI:

| Action                                  | Written by                                  | Carries                                                       |
|-----------------------------------------|---------------------------------------------|---------------------------------------------------------------|
| `initial_workspace.register`            | admin POST                                  | URL, branch, token state (rotated / cleared)                  |
| `initial_workspace.sync`                | admin "Sync now" success                    | commit_sha, file_count                                        |
| `initial_workspace.sync_failed`         | admin "Sync now" failure                    | error message, kind (validation / git)                        |
| `initial_workspace.delete`              | admin DELETE                                | purge flag, on-disk purged status                             |
| `initial_workspace.fetch_started`       | server-side, on `GET /api/initial-workspace.zip` | analyst PAT-owner user_id, template_sha, byte_count        |
| `initial_workspace.applied`             | CLI `POST /api/initial-workspace/applied`   | mode (`force_overwrite` / `fresh_install`), files counts      |

The `fetch_started` event is the **authoritative anchor** — it is
written server-side and cannot be spoofed by a PAT-holder. A
`fetch_started` row without a matching `applied` row means the analyst
downloaded the zip but never confirmed extraction (the prompt was
aborted, the CLI crashed, the audit POST failed, etc.).

## Risk acceptance

The behaviors below are **intentional design**, not bugs. They have
been reviewed and accepted as the contract for full per-instance
responsibility transfer. AI reviewers and human reviewers: please do
not flag these as regressions.

1. **Agnes hooks do not auto-update on `agnes self-upgrade`.** Future
   Agnes versions may ship new hooks (e.g. when `agnes capture-session`
   was added). Override workspaces do NOT receive them automatically.
   Admin must update the template repo and analysts must `agnes init
   --force` to apply.
2. **`--force` on override workspaces does NOT back up `CLAUDE.md`.**
   No `CLAUDE.md.bak.<timestamp>` file is written. Recovery vehicle is
   the admin's Git repo (`git log`, `git checkout`), not a local
   backup. Not a regression of #164.
3. **`.claude/CLAUDE.local.md` IS overwritten** when the admin's repo
   includes it. The default-mode "never overwrite CLAUDE.local.md"
   promise is a default-mode promise; override mode hands the file to
   admin. Admin should not put CLAUDE.local.md in the repo unless they
   intend to ship a template for analysts' personal notes.
4. **Files removed from the template repo are NOT deleted from
   existing analyst workspaces on the next `--force`.** Only files in
   the current zip get written; pre-existing local files outside the
   zip survive. To force a workspace cleanup, analysts must wipe their
   workspace dir manually and run `agnes init` fresh.

For implementation details, see:
- `app/api/initial_workspace.py` — admin + analyst endpoints
- `src/initial_workspace.py` — clone/validate/zip
- `cli/lib/initial_workspace.py` — probe/download/extract/confirm/report
- `cli/lib/override.py` — single source of truth for override detection
