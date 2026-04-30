# Architecture — Detailed Reference

Comprehensive architectural overview of the AI Data Analyst platform (v2).

## Top-Level Module Map

```
ai-data-analyst/
├── src/                  Core engine (db, orchestrator, rbac, profiler, repositories)
├── connectors/           Pluggable data connectors (keboola, bigquery, jira, llm, openmetadata)
├── app/                  FastAPI application (API + web UI)
│   ├── api/              REST API routers
│   ├── auth/             Auth providers (JWT, Google OAuth, email magic link, password)
│   └── web/              HTML dashboard routes
├── services/             Standalone background services (scheduler, telegram_bot, ws_gateway, …)
├── cli/                  CLI tool (da sync, da query, da admin)
├── scripts/              Utility and migration scripts
├── config/               Instance configuration templates
├── tests/                Test suite
└── docs/                 User-facing documentation
```

## System Overview

```
┌─────────────────────────────────────────────────────────────────┐
│  EXTERNAL DATA SOURCES                                          │
│  Keboola Storage  │  BigQuery  │  Jira Cloud  │  CSV/files     │
└──────────┬────────┴─────┬──────┴──────┬────────┴────────────────┘
           │              │             │
           ▼              ▼             ▼
┌─────────────────────────────────────────────────────────────────┐
│  CONNECTORS  (connectors/)                                      │
│  extractor.py per source → extract.duckdb contract             │
└──────────────────────────┬──────────────────────────────────────┘
                           │  /data/extracts/{source}/extract.duckdb
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│  SYNC ORCHESTRATOR  (src/orchestrator.py)                       │
│  Scans extracts/, ATTACHes each extract.duckdb,                │
│  creates master views in analytics.duckdb (atomic swap)        │
└──────────────────────────┬──────────────────────────────────────┘
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
     ┌──────────────┐  ┌────────┐  ┌──────────────┐
     │  FastAPI app  │  │  CLI   │  │  Scheduler   │
     │  port 8000    │  │  `da`  │  │  sidecar     │
     └──────────────┘  └────────┘  └──────────────┘
              │
    ┌─────────┴──────────┐
    ▼                    ▼
system.duckdb       analytics.duckdb
(state/registry)    (master views)
```

**Deployment:** Docker Compose. The `app` service runs Uvicorn. The `scheduler` sidecar triggers
sync jobs via the app's REST API. Optional `full` profile adds telegram-bot, ws-gateway,
corporate-memory, session-collector.

```bash
docker compose up               # app + scheduler
docker compose --profile full up  # all services
docker compose --profile extract run extract  # one-shot extraction
```

---

## extract.duckdb Contract

Every connector writes to the same directory layout:

```
/data/extracts/{source_name}/
├── extract.duckdb          ← _meta table + views over parquet files
└── data/                   ← parquet files (local connectors only)
    ├── table_a.parquet
    └── table_b.parquet
```

### _meta table

Required in every `extract.duckdb`:

```sql
CREATE TABLE _meta (
    table_name   VARCHAR NOT NULL,
    description  TEXT,
    rows         BIGINT,
    size_bytes   BIGINT,
    extracted_at TIMESTAMP,
    query_mode   VARCHAR    -- 'local' or 'remote'
);
```

The orchestrator reads `_meta` to know which tables exist and creates a corresponding
view in `analytics.duckdb` for each row.

### _remote_attach table (optional)

Connectors whose views reference an external DuckDB extension (e.g. Keboola, BigQuery)
must include this table so the orchestrator can re-ATTACH the external source at rebuild time:

```sql
CREATE TABLE _remote_attach (
    alias     VARCHAR,  -- DuckDB alias for the attached source, e.g. 'kbc'
    extension VARCHAR,  -- Extension name, e.g. 'keboola'
    url       VARCHAR,  -- Connection URL
    token_env VARCHAR   -- Name of the env var holding the auth token
);
```

The orchestrator installs/loads the extension, reads the token from the environment, and
ATTACHes the external source so remote views resolve correctly. This mechanism is generic —
any connector can use it. Auth credentials are never stored in `extract.duckdb`.

---

## SyncOrchestrator

`src/orchestrator.py` — thread-safe via `_rebuild_lock`.

### rebuild()

1. Open a **temporary** DuckDB file (`analytics.duckdb.tmp`).
2. Scan `/data/extracts/*/extract.duckdb` (sorted, skips non-directories and missing files).
3. Validate each directory name as a safe SQL identifier (`^[a-zA-Z_][a-zA-Z0-9_]{0,63}$`).
4. For each source: `ATTACH '{db_file}' AS {source_name} (READ_ONLY)`.
5. Handle `_remote_attach` — install extension, read token from env, ATTACH external source.
6. Read `_meta`, validate each `table_name` identifier, create `CREATE OR REPLACE VIEW`.
7. Update `sync_state` in `system.duckdb` (mtime-based hash, no full file read).
8. `CHECKPOINT` and close the temp connection.
9. **Atomic swap**: `shutil.move(tmp_path, target_path)` — replaces `analytics.duckdb` in-place.

### rebuild_source(source_name)

Convenience wrapper that calls `rebuild()` in full (partial rebuild is not possible because
`analytics.duckdb` is written fresh from scratch each time). Used after Jira webhooks.

### Identifier validation

Both `source_name` and `table_name` are checked against `^[a-zA-Z_][a-zA-Z0-9_]{0,63}$`
before being interpolated into SQL. Invalid names are skipped with a warning.

---

## Data Sources

### Keboola — Batch Pull

`connectors/keboola/extractor.py`

- Uses the DuckDB Keboola community extension to download tables directly to parquet.
- Fallback path: `connectors/keboola/client.py` (Keboola Storage API wrapper).
- Sync strategies: `full_refresh`, `incremental`, `partitioned`.
- Writes `extract.duckdb` + `data/*.parquet` under `/data/extracts/keboola/`.
- For tables with `query_mode='remote'`, populates `_remote_attach` so views proxy queries
  to Keboola rather than downloading data locally.

Sync trigger flow:

```
POST /api/sync/trigger (admin)
  → BackgroundTask: _run_sync()
    → Read table_registry from system.duckdb (main process)
    → Serialize configs as JSON, spawn subprocess (no DuckDB lock conflict)
    → Subprocess: connectors/keboola/extractor.run()  →  extract.duckdb
    → SyncOrchestrator().rebuild()  →  analytics.duckdb
    → Profiler: profile each synced parquet  →  table_profiles
```

### BigQuery — Remote Attach

`connectors/bigquery/extractor.py`

- Uses the DuckDB BigQuery community extension.
- No data download — views proxy all queries directly to BigQuery.
- Auth via `GOOGLE_APPLICATION_CREDENTIALS` (service account JSON) or ADC.
- Populates `_remote_attach` with `extension='bigquery'` and no `token_env` (env-based auth).

### Jira — Real-Time Push

`connectors/jira/webhook.py` → `incremental_transform.py` → `extract_init.py`

```
Jira Cloud webhook (issue created/updated/deleted)
  → POST /api/jira/webhook  (HMAC-SHA256 verification)
  → connectors/jira/webhook.py  (validate, persist raw JSON)
  → connectors/jira/incremental_transform.py  (update monthly parquet shards)
  → extract_init.py  (update _meta)
  → SyncOrchestrator().rebuild_source('jira')
```

Output tables (6): `issues`, `comments`, `attachments`, `changelog`, `issuelinks`, `remote_links`.

Background supplements:
- `jira-sla-poll` — refreshes SLA fields for open tickets every 5 min.
- `jira-consistency` — detects and backfills missing issues every 6 h.

Files NOT to modify: `connectors/jira/file_lock.py`, `connectors/jira/transform.py`.

---

## DuckDB Schema

### system.duckdb — `{DATA_DIR}/state/system.duckdb`

Current schema version: **19** (auto-migrated from any earlier version on startup — see `src/db.py`).

| Table | Purpose |
|-------|---------|
| `schema_version` | Tracks applied migration version |
| `users` | Registered users: id, email, name, password_hash, setup/reset tokens, active flag |
| `user_groups` | Named groups (`Admin`, `Everyone` seeded as `is_system=TRUE`; admin-managed and Google-synced groups) |
| `user_group_members` | `(user_id, group_id, source)` — `source ∈ {admin, google_sync, system_seed}` |
| `resource_grants` | Generic per-`(group, resource_type, resource_id)` grants (replaces `dataset_permissions` + `plugin_access`) |
| `sync_state` | Per-table sync status: last_sync, rows, file_size_bytes, hash, status |
| `sync_history` | Historical sync runs with duration and error |
| `user_sync_settings` | Per-user dataset enable/disable preferences |
| `table_registry` | Registered tables: source_type, bucket, source_table, query_mode, sync_schedule |
| `table_profiles` | JSON data profiles (stats, nulls, cardinality) per table |
| `knowledge_items` | Corporate memory knowledge entries (V1 columns: `confidence`, `domain`, `entities`, `source_type`, `source_ref`, `valid_from`/`valid_until`, `supersedes`, `sensitivity`, `is_personal`) |
| `knowledge_votes` | Up/down votes on knowledge items |
| `knowledge_contradictions` | Pairs of items the LLM judge flagged as contradictory; carries `severity` and `suggested_resolution` (JSON-encoded structured action — see ADR Decision 4) |
| `verification_evidence` | One row per detected verification — persists `user_quote`, `detection_type`, and `source_ref` so future Bayesian re-calibration has raw signal (ADR Decision 3) |
| `session_extraction_state` | Tracks which `/data/user_sessions/*.jsonl` files have been processed by the verification detector |
| `audit_log` | API action log: user, action, resource, duration |
| `telegram_links` | Telegram chat_id linked to user_id |
| `pending_codes` | Telegram link confirmation codes |
| `script_registry` | Deployed Python notification scripts |

Connections: `get_system_db()` returns a cursor on a **single shared connection** per
`DATA_DIR` (protected by `threading.Lock`). Callers `close()` the cursor, not the
underlying connection. This avoids DuckDB write-lock conflicts in the multi-threaded
FastAPI process.

### analytics.duckdb — `{DATA_DIR}/analytics/server.duckdb`

Read-only views over all ATTACHed `extract.duckdb` sources. Rebuilt atomically by
`SyncOrchestrator.rebuild()`. Query endpoints open this file via `get_analytics_db_readonly()`
which ATTACHes all `extract.duckdb` files in read-only mode so remote views resolve correctly.

---

## Authentication

All auth flows issue a **JWT** (`app/auth/jwt.py`) stored as a cookie (`access_token`) or
passed as a `Bearer` token in the `Authorization` header. The `get_current_user` dependency
validates the JWT and loads the user from `users` in `system.duckdb`.

### Providers (`app/auth/providers/`)

| Provider | Available when | Flow |
|----------|---------------|------|
| `google.py` | `GOOGLE_CLIENT_ID` + `GOOGLE_CLIENT_SECRET` set | Google OAuth 2.0 / OIDC (Authlib). Domain restriction via `allowed_domains` in `instance.yaml`. Callback issues JWT cookie. |
| `email.py` | `SMTP_HOST` or `SENDGRID_API_KEY` set | Magic link: `POST /auth/email/send-link` generates a token stored in `users.setup_token`; `POST /auth/email/verify` exchanges it for a JWT. |
| `password.py` | Always registered | Email + password with hashed credentials. |

### RBAC

Two layers, no role hierarchy (see `docs/RBAC.md` for the full reference):

- **App-level access**: membership in the `Admin` system group. The
  `require_admin` FastAPI dependency in `app.auth.access` gates admin
  endpoints (admin UI, user management, settings, …).
- **Resource-level access**: per-(group, resource_type, resource_id)
  grants in `resource_grants`. The `require_resource_access(rt,
  path_template)` dependency factory gates entity-scoped endpoints.

Table access (`src/rbac.py:can_access_table`) is a thin wrapper over
`app.auth.access.can_access(user_id, "table", table_id, conn)`. Admin
group members short-circuit; everyone else needs an explicit
`resource_grants(group, "table", table_id)` row via any group they
belong to. There is no `is_public` shortcut and no implicit "Everyone
can read" fallback — the legacy `dataset_permissions` + `is_public`
mechanism was dropped in v19.

---

## API Layer

All routes are FastAPI `APIRouter` instances registered in `app/main.py`.

### REST API (`app/api/`)

| Router | Prefix | Key endpoints |
|--------|--------|---------------|
| `sync` | `/api/sync` | `GET /manifest` (hash manifest, per-user filtered), `POST /trigger` (admin), `GET/POST /settings`, `GET/POST /table-subscriptions` |
| `data` | `/api/data` | Download parquet files for synced tables |
| `query` | `/api/query` | `POST /` — execute a SELECT against `analytics.duckdb` (sandbox enforced) |
| `admin` | `/api/admin` | `GET /discover-tables`, `GET /registry`, `POST /register-table`, `PUT /registry/{id}`, `DELETE /registry/{id}` |
| `catalog` | `/api/catalog` | Data catalog: table list, profiles, metric definitions |
| `users` | `/api/users` | User CRUD (admin), self-service profile |
| `permissions` | `/api/permissions` | Dataset permission grants (admin) |
| `access_requests` | `/api/access-requests` | Request + review workflow |
| `scripts` | `/api/scripts` | Deploy, list, run, delete Python notification scripts |
| `settings` | `/api/settings` | Instance and user settings |
| `memory` | `/api/memory` | Corporate memory CRUD and voting |
| `upload` | `/api/upload` | File upload (CSV, parquet) |
| `telegram` | `/api/telegram` | Telegram account link/unlink |
| `jira_webhooks` | `/api/jira` | Jira webhook receiver (HMAC-SHA256 verified) |
| `health` | `/api/health` | Service health, sync status, disk |

### Auth routes (`app/auth/`)

`POST /auth/token`, `GET /auth/me`, `POST /auth/logout`,
`GET /auth/google/login`, `GET /auth/google/callback`,
`POST /auth/email/send-link`, `POST /auth/email/verify`,
`POST /auth/password/login`

### Web UI (`app/web/`)

HTML dashboard routes served by Jinja2 templates. Registered last (catch-all).

---

## Services

Each service is a self-contained Python package (`services/<name>/__main__.py`) run as a
Docker Compose service.

| Service | Profile | Schedule / Mode | Description |
|---------|---------|-----------------|-------------|
| `scheduler` | default | Always-on; polls every N seconds | Lightweight sidecar that triggers jobs via the app's REST API (`POST /api/sync/trigger` every 15 min, `GET /api/health` every 5 min). Auth via `SCHEDULER_API_TOKEN` or auto-fetch from `/auth/token`. |
| `telegram_bot` | `full` | Always-on (long-poll) | Telegram bot: polling + HTTP dispatch, `/status` command, notification script execution. |
| `ws_gateway` | `full` | Always-on | WebSocket gateway (TCP 8765) + HTTP dispatch socket. JWT auth. Per-user connection limit (5). Heartbeat ping/pong. |
| `corporate_memory` | `full` | Periodic (every 30 min) | Scans `CLAUDE.local.md` files, extracts knowledge via LLM (Claude Haiku), writes to `knowledge_items` in system.duckdb. Inline contradiction detection runs after each new item: one batched Haiku structured-output call returns judgments + structured resolution suggestions for every same-domain candidate (no SQL keyword pre-filter — see [ADR Decision 4](ADR-corporate-memory-v1.md)). |
| `verification_detector` | `full` (run via `corporate_memory`) | On each `corporate_memory` tick | Scans unprocessed analyst session JSONLs, extracts corrections / confirmations / unprompted definitions via Haiku structured outputs. Confidence is computed in code from `(source_type, detection_type)` — never trusted from the LLM. Each verification persists a `verification_evidence` row carrying `user_quote` + `detection_type` ([ADR Decision 3](ADR-corporate-memory-v1.md)). |
| `session_collector` | `full` | Periodic (every 6 h) | Copies Claude Code `.jsonl` session transcripts to central storage. |

Files NOT to modify: `services/ws_gateway/` (stable WebSocket infrastructure).

### Corporate-memory privacy boundary

`is_personal` on `knowledge_items` is enforced as an authorization rule at every read site, not a UI hint:

- `GET /api/memory` and `GET /api/memory?search=…` silently coerce `exclude_personal=True` for any caller whose role is below `km_admin`.
- `GET /api/memory/{id}/provenance` and `POST /api/memory/{id}/vote` use the shared `_can_view_item(user, item)` helper (`not is_personal OR contributor OR km_admin/admin`) and return **404** (not 403) on denial to avoid existence-leak.
- Contributors reach their own personal items via `/api/memory/my-contributions`.

See [ADR Decision 1](ADR-corporate-memory-v1.md) for the full reasoning.

---

## Security

### Query Sandbox (`app/api/query.py`)

The `/api/query` endpoint enforces a strict SQL allowlist:

- Only `SELECT` and `WITH` queries accepted.
- Blocklist of ~30 keywords/functions: `DROP`, `DELETE`, `INSERT`, `UPDATE`, `ALTER`,
  `CREATE`, `ATTACH`, `DETACH`, `LOAD`, `INSTALL`, `COPY`, `PRAGMA`, file functions
  (`read_parquet`, `read_csv`, `glob`, etc.), URL schemes (`s3://`, `gcs://`, `http://`),
  and multi-statement separator (`;`).
- Table-level RBAC: forbidden views are detected by word-boundary regex match against
  the SQL text. Query is rejected if user lacks access to any referenced table.
- Analytics DB opened in `read_only=True` mode per request.

### Script Sandbox (`app/api/scripts.py`)

Deployed and ad-hoc Python scripts are checked against a pattern blocklist before execution:

- Blocked: `subprocess`, `shutil`, `ctypes`, `importlib`, `socket`, `requests`, `httpx`,
  `urllib`, `os`, `sys`, `signal`, `open(`, `pathlib`, `exec(`, `eval(`, `compile(`,
  `__import__`, and others.
- Scripts run in a subprocess with a configurable timeout (`SCRIPT_TIMEOUT`, default 300 s)
  and capped output (`SCRIPT_MAX_OUTPUT`, default 64 KB).

### Identifier Validation (`src/orchestrator.py`, `src/db.py`)

All dynamic SQL identifiers (source names, table names, extension aliases) are validated
against `^[a-zA-Z_][a-zA-Z0-9_]{0,63}$` before interpolation. Invalid identifiers are
skipped with a log warning, never executed.

### Authentication Layers

| Layer | Mechanism |
|-------|-----------|
| Web UI / API | JWT Bearer token or `access_token` cookie |
| Google OAuth | Authlib OIDC + domain allowlist |
| Email magic link | `secrets.token_urlsafe(32)` stored in `users.setup_token`, 1-hour expiry |
| Jira webhook | HMAC-SHA256 signature verification |
| Inter-service (scheduler) | `SCHEDULER_API_TOKEN` env var or auto-fetched JWT |

---

## Configuration

```
config/instance.yaml             (instance-specific, not committed)
    │ loaded by config/loader.py
    │ ${ENV_VAR} references resolved from .env
    ▼
app/instance_config.py           (exposes get_data_source_type(), get_allowed_domains(), get_value())
    ▼
FastAPI dependency injection     (passed to API routers as needed)
```

Table configuration lives in `table_registry` inside `system.duckdb`, not in static files.
Use `POST /api/admin/register-table` or the web UI admin panel to register tables.

Required env vars: `DATA_DIR`, `JWT_SECRET_KEY`. Source-specific vars (`KEBOOLA_STORAGE_TOKEN`,
`GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `SMTP_HOST` / `SENDGRID_API_KEY`, etc.) are
optional and gate the relevant connectors/providers.

---

## Data Filesystem Layout

```
/data/
├── state/
│   └── system.duckdb          user registry, sync state, table_registry, audit log
├── analytics/
│   └── server.duckdb          master analytics DB (views over all extracts)
└── extracts/
    ├── keboola/
    │   ├── extract.duckdb     _meta + views
    │   └── data/*.parquet
    ├── bigquery/
    │   └── extract.duckdb     _meta + _remote_attach + remote views
    └── jira/
        ├── extract.duckdb     _meta + views
        └── data/*.parquet
```

---

## Extending the Platform

### New Data Source

1. Create `connectors/<name>/extractor.py`.
2. Write `extract.duckdb` with `_meta` table and views/tables.
3. Add `data/*.parquet` for local sources.
4. Add `_remote_attach` row if views reference an external DuckDB extension.
5. `SyncOrchestrator` picks it up automatically on next `rebuild()`.

### New Auth Provider

1. Add `app/auth/providers/<name>.py` exporting a FastAPI `APIRouter`.
2. Register the router in `app/main.py`.
3. All providers must issue a JWT and set the `access_token` cookie on success.
