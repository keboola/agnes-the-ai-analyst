# Marketplace migration into agnes-the-ai-analyst — design

**Date:** 2026-04-24
**Status:** approved
**Scope:** absorb `marketplace-server`'s zip + git-smart-HTTP distribution into agnes's FastAPI app, reuse agnes's PAT auth, drop the separate service.

## Motivation

`marketplace-server/` is a standalone FastAPI PoC that serves per-user Claude Code plugin marketplaces through two delivery paths (ZIP + git smart-HTTP). It has its own auth scheme ("email as credential"), its own config, its own Docker service, and bind-mounts the source marketplace repo. Everything it does belongs inside agnes:

- agnes already has production-grade auth (Google OAuth, email magic link, password, and **Personal Access Tokens**).
- agnes already has a users table, session middleware, and a FastAPI router pattern.
- Running two servers with separate auth, separate config, separate compose setup for a feature that is purely "serve files to authenticated users" is redundant.

After this migration, agnes gains three endpoints under `/api/marketplace/*`, marketplace-server/ can be deleted, and operators no longer maintain a second service.

## Goals

- Three endpoints on agnes serving per-user marketplaces based on role/group membership:
  - `GET /api/marketplace/info` — JSON describing the caller's allowed plugins.
  - `GET /api/marketplace/zip` — the filtered marketplace as a deterministic ZIP.
  - `/api/marketplace/git/*` — git smart-HTTP for `/plugin marketplace add <url>`.
- Authentication via agnes's existing **Personal Access Token** (PAT) system.
- Byte-identical behavior to marketplace-server for a given user and source snapshot (same ETag/commit SHA).
- Marketplace code lives in its own sub-package so it's easy to reason about and, if ever needed, extract again.

## Non-goals

- No DuckDB-backed group store; `user_groups.json` and `group_plugins.json` stay as static config files (explicit user preference).
- No admin UI for editing groups/plugins.
- No auto-cloning of the source marketplace; the operator populates `/data/marketplace/source/`.
- Not deleting `marketplace-server/` in this change — user will remove it after verifying the migration works.
- Not updating the sync script in `agnes-marketplace-client/` in this change; that's a separate follow-up (endpoints move from `/marketplace.zip` to `/api/marketplace/zip`).

## Architecture

### File layout

Everything marketplace-related lives under a single sub-package so the boundary is obvious:

```
agnes-the-ai-analyst/
├── app/api/marketplace/
│   ├── __init__.py              # exports `router: APIRouter` and `make_git_wsgi_app()`
│   ├── info.py                  # GET /api/marketplace/info           (FastAPI)
│   ├── zip.py                   # GET /api/marketplace/zip            (FastAPI)
│   ├── git.py                   # WSGI app, mounted at /api/marketplace/git by app.main
│   ├── _packager.py             # ported from marketplace-server/app/packager.py
│   ├── _git_backend.py          # ported from marketplace-server/app/git_backend.py
│   └── _auth.py                 # PAT-first / email-fallback resolver (shared by all 3)
├── config/marketplace/
│   ├── user_groups.json         # copied from marketplace-server/config/
│   └── group_plugins.json       # copied from marketplace-server/config/
└── tests/marketplace/
    ├── conftest.py              # ported; temp source + config fixtures
    ├── test_packager.py         # renamed from marketplace-server tests/test_smoke.py
    ├── test_git_backend.py
    ├── test_git_router.py
    └── test_integration.py      # adapted: /api/marketplace/* paths + PAT auth
```

Wiring points in existing agnes code:

- `app/main.py` — register the router and mount the git WSGI app.
- `pyproject.toml` — add `dulwich>=0.22` and `a2wsgi>=1.10` to `[project.dependencies]`.
- `docker-compose.yml` — add marketplace env vars to the `app` service; **no new containers**.

### Module boundaries

| Module | Responsibility | Depends on |
|---|---|---|
| `_packager.py` | read config + source, compute ETag, build filtered `marketplace.json`, build ZIP | env vars, `config/marketplace/*`, `/data/marketplace/source/` |
| `_git_backend.py` | materialize cached bare git repo per distinct group-set, atomic rename-into-cache | `_packager`, `dulwich` |
| `_auth.py` | resolve caller → email from PAT or (optional) email fallback, for both FastAPI and WSGI | `app.auth.jwt.verify_token` |
| `info.py` / `zip.py` | thin FastAPI handlers calling `_packager.build_info` / `build_zip` | `_packager`, `_auth` |
| `git.py` | WSGI app: Basic auth → `_auth.resolve_email_from_basic` → `_git_backend.ensure_repo_for_email` → dulwich `HTTPGitApplication` | `_git_backend`, `_auth`, `dulwich`, `a2wsgi` |

Private modules are underscore-prefixed because nothing outside `app/api/marketplace/` should import them.

## Authentication

### Primary path — Personal Access Token (PAT)

Agnes already issues PATs: a PAT is a JWT signed with `JWT_SECRET_KEY`, carrying `{sub, email, role, typ: "pat", jti, iat}`, with revocation/expiry tracked in the `personal_access_tokens` table.

- **FastAPI endpoints** (`info`, `zip`) — use `Depends(get_current_user)` from `app.auth.dependencies`. This inherits the existing PAT validation (signature + DB revocation check + expiry + token-hash match + LOCAL_DEV_MODE bypass). Extract `user["email"]`.
- **WSGI endpoint** (`git`) — git CLI sends credentials via HTTP Basic where the *password* field carries the PAT (`https://x:<PAT>@host/api/marketplace/git`). The WSGI layer is sync and can't `await` the async FastAPI dependency, so `_auth` provides a sync helper that:
  1. Parses `Authorization: Basic <base64>`, extracts password.
  2. If password looks like a JWT (contains `.` and is not an email), call `verify_token(password)` to get the payload; use `payload["email"]` directly.
  3. Optionally (for defense-in-depth) open a short-lived DuckDB connection and check the PAT's revocation state via `AccessTokenRepository`. **Deferred to follow-up** — initial cut relies on signature verification only, matching the trust level marketplace-server already accepts. A comment in the code flags this for later hardening.

### Temporary fallback — email as credential

Gated by `MARKETPLACE_ALLOW_EMAIL_AUTH=1` (default off). When enabled:

- `info` / `zip`: accept `?email=<email>` query param (current marketplace-server behavior).
- `git`: accept a plain email in the Basic auth password field (existing marketplace-server scheme).

Detection — `_auth` classifies the credential:
- Contains `@` → treat as email (fallback path).
- Contains `.` and decodes as a valid JWT → treat as PAT.
- Else → 401.

When the fallback flag is off, email-shaped credentials are rejected with 401. This lets the migration ship in "parallel" mode (existing clients using emails still work while they migrate to PATs), then the flag flips off and one env-var-deletion removes the PoC path entirely.

### LOCAL_DEV_MODE

Agnes's existing `LOCAL_DEV_MODE=1` auto-authenticates every request as `dev@localhost`. The FastAPI endpoints inherit this for free via `get_current_user`. The git WSGI endpoint must handle it explicitly — if `LOCAL_DEV_MODE` is on and no credentials are supplied, treat the caller as `dev@localhost`. This keeps dev parity with the rest of agnes.

### Summary table

| Endpoint | Auth mechanism (primary) | Auth mechanism (fallback, env-gated) |
|---|---|---|
| `GET /api/marketplace/info` | `Authorization: Bearer <PAT>` or session cookie | `?email=<email>` |
| `GET /api/marketplace/zip` | `Authorization: Bearer <PAT>` or session cookie | `?email=<email>` |
| `/api/marketplace/git/*` | HTTP Basic, password = PAT | HTTP Basic, password = email |

## Paths and config

| Purpose | Path | Env var (override) |
|---|---|---|
| Source marketplace (read-only) | `/data/marketplace/source/` | `MARKETPLACE_SOURCE_PATH` |
| Per-group bare-repo cache | `/data/marketplace/cache/` | `MARKETPLACE_CACHE_DIR` |
| `email → [groups]` config | `config/marketplace/user_groups.json` | `MARKETPLACE_USER_GROUPS_PATH` |
| `group → plugin spec` config | `config/marketplace/group_plugins.json` | `MARKETPLACE_GROUP_PLUGINS_PATH` |
| Enable email-as-credential fallback | — | `MARKETPLACE_ALLOW_EMAIL_AUTH=1` |

The source directory is populated by the operator (same pattern as `/data/extracts/`). If missing, endpoints return 503 with a clear message.

The cache is safe to wipe — it regenerates from source + config on next request.

Config files are re-read on every request, matching marketplace-server's behavior. Edits take effect immediately without restart.

## Data flow

### `GET /api/marketplace/info` (PAT)

1. `get_current_user` validates PAT → user dict from `users` table.
2. Handler reads `user["email"]`.
3. `_packager.build_info(email)` loads `user_groups.json` → groups, loads `group_plugins.json` → allowed plugin names, reads `/data/marketplace/source/.claude-plugin/marketplace.json`, computes ETag, returns dict.
4. Response: JSON (same shape as marketplace-server).

### `GET /api/marketplace/zip` (PAT)

1. Same auth + email resolution as `info`.
2. `_packager.build_zip(email)` assembles deterministic ZIP bytes.
3. If `If-None-Match` matches current ETag → 304.
4. Else → 200 with `ETag`, `Content-Disposition`, `application/zip` body.

### `/api/marketplace/git/*` (PAT)

1. Client runs `git clone https://x:<PAT>@host/api/marketplace/git`.
2. WSGI app extracts Basic auth password.
3. `_auth.resolve_email_from_basic(password)`:
   - PAT → `verify_token` → `payload["email"]`.
   - Email (fallback enabled) → passthrough after `is_known_email` check.
   - Missing + `LOCAL_DEV_MODE` → `dev@localhost`.
   - Otherwise → None → 401 with `WWW-Authenticate: Basic realm="agnes-marketplace"`.
4. `_git_backend.ensure_repo_for_email(email)` atomically materializes the bare repo for the caller's group-set (keyed by content hash).
5. Delegate to `dulwich.web.HTTPGitApplication` scoped to that repo; wrap in `_CloseOnExhaust` so the repo closes after the response body drains.

### ETag / cache key

Unchanged from marketplace-server: `sha256(canonical_json)[:16]` where the canonical JSON covers the source marketplace version + each allowed plugin's files (sorted) + `global-rules/` files. The ZIP uses it as the HTTP ETag; the git cache uses it as the bare-repo dirname. Same hash = same bytes = same commit SHA.

## Error handling

| Condition | Response |
|---|---|
| Missing source dir / `marketplace.json` | 503 `"marketplace source unavailable"` (new — marketplace-server currently 500s) |
| Missing `config/marketplace/*.json` | 500 (operator error; log loudly) |
| Missing / invalid PAT (fallback off) | 401 |
| Email fallback off + `?email=` passed | 401 |
| Email fallback on + unknown email (zip/info) | default group `grp_foundryai_everyone` (preserves current behavior) |
| Email fallback on + unknown email (git) | 401 (preserves current fail-closed for git) |
| User authenticated but has no entry in `user_groups.json` | default group `grp_foundryai_everyone` |
| Source plugin listed in `marketplace.json` but `plugin.json` missing | log + keep listed version (existing behavior) |

## Testing

Port all four test files from `marketplace-server/tests/` into `tests/marketplace/` and adapt:

- `test_packager.py` (renamed from `test_smoke.py`) — no URL or auth changes; fixtures repoint to agnes paths.
- `test_git_backend.py` — unchanged logic; fixtures repoint.
- `test_git_router.py` — update to use the `_auth` resolver; add a PAT case alongside the email case.
- `test_integration.py` — adapted: new URL prefix (`/api/marketplace/*`), PAT auth via `create_access_token` from `app.auth.jwt`, the email-fallback case gated by env.

New cases specifically for the migration:

- PAT-authenticated `/api/marketplace/info` returns same payload as fallback-email call for the same user.
- PAT-authenticated `/api/marketplace/zip` + `/api/marketplace/git` return same ETag / same commit SHA as the fallback-email call.
- Email fallback is rejected (401) when `MARKETPLACE_ALLOW_EMAIL_AUTH` is unset.
- `LOCAL_DEV_MODE` auto-authenticates on all three endpoints as `dev@localhost`.

Fixtures stay self-contained (temp source + temp config per test), so the suite never touches `/data/marketplace/source/` or the real `config/marketplace/`.

## Dependencies

Add to `pyproject.toml` `[project.dependencies]`:

```
"dulwich>=0.22",
"a2wsgi>=1.10",
```

Both are self-contained pure-Python packages and don't conflict with agnes's existing deps.

## Deployment

No new Docker services. `docker-compose.yml` gains env vars on the existing `app` service:

```yaml
environment:
  - DATA_DIR=/data
  - MARKETPLACE_SOURCE_PATH=/data/marketplace/source
  - MARKETPLACE_CACHE_DIR=/data/marketplace/cache
  # Temporary — set to 1 during migration to accept legacy email credentials.
  - MARKETPLACE_ALLOW_EMAIL_AUTH=${MARKETPLACE_ALLOW_EMAIL_AUTH:-0}
```

Operator actions on first deploy:

1. `mkdir -p /data/marketplace/source`
2. Clone or sync the source marketplace repo into that path.
3. Ensure `config/marketplace/user_groups.json` and `group_plugins.json` are present.
4. Start agnes as usual.

## Out of scope / follow-ups

- **Remove `marketplace-server/`.** The old directory and its Docker service remain on disk; delete after verifying the migration works end-to-end.
- **Update `agnes-marketplace-client/scripts/sync.py`.** The sync script has the old URL hard-coded; needs updating to `/api/marketplace/zip` and PAT auth.
- **Re-register Claude Code marketplaces.** Existing `/plugin marketplace add https://.../marketplace.git` registrations need to be re-added with the new URL (`/api/marketplace/git`) and a PAT.
- **DB-backed PAT revocation for the git endpoint.** Initial cut verifies JWT signature only (same effective trust level as marketplace-server's email scheme). Future: hit `personal_access_tokens` from the WSGI layer for revocation/expiry.
- **Flip off `MARKETPLACE_ALLOW_EMAIL_AUTH`.** After all clients migrate, unset the env var; the fallback code path can be deleted in a subsequent small change.
