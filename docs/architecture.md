# Architecture — Detailed Reference

Comprehensive architectural overview of the OSS AI Data Analyst platform.
For a concise summary, see [../ARCHITECTURE.md](../ARCHITECTURE.md).

## Top-Level Module Map

```
oss-ai-data-analyst/
├── src/                  Core engine (config, sync, parquet, profiling)
├── connectors/           Pluggable data connectors (keboola, jira)
├── auth/                 Pluggable auth providers (google, password, desktop)
├── services/             Standalone background services
├── webapp/               Flask web portal (dashboard, catalog, API)
├── server/               Server deployment (setup, deploy, nginx, systemd)
├── scripts/              Analyst-side utility scripts (sync, DuckDB, dev server)
├── config/               Instance configuration (loader, templates)
├── examples/             Example notification scripts
├── tests/                Test suite
├── dev_docs/             Internal development documentation
└── docs/                 User-facing documentation
```

## Block Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         EXTERNAL DATA SOURCES                               │
│   ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐              │
│   │ Keboola  │   │   Jira   │   │   CSV    │   │ BigQuery │              │
│   │ Storage  │   │  Cloud   │   │  (plan)  │   │  (plan)  │              │
│   └────┬─────┘   └────┬─────┘   └──────────┘   └──────────┘              │
└────────┼──────────────┼────────────────────────────────────────────────────┘
         │              │
         ▼              ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  CONNECTORS  (connectors/)                  auto-discovery via importlib    │
│                                                                             │
│  ┌──────────────────────────┐    ┌─────────────────────────────────────┐   │
│  │ connectors/keboola/       │    │ connectors/jira/                    │   │
│  │                           │    │                                     │   │
│  │ adapter.py                │    │ webhook.py    Flask blueprint       │   │
│  │  KeboolaDataSource (ABC)  │    │ service.py    Jira REST API client │   │
│  │  full/incr/partitioned    │    │ transform.py  JSON -> 6 Parquet tbl│   │
│  │                           │    │ incremental_transform.py  realtime │   │
│  │ client.py                 │    │ file_lock.py  POSIX advisory locks │   │
│  │  Keboola Storage API      │    │                                     │   │
│  │  type mapping + cache     │    │ scripts/  backfill, SLA poll,      │   │
│  │                           │    │           consistency check         │   │
│  │ tests/                    │    │ systemd/  jira-sla-poll,           │   │
│  └──────────────────────────┘    │           jira-consistency          │   │
│                                   │ tests/                              │   │
│  Registry: src/data_sync.py       └─────────────────────────────────────┘   │
│  create_data_source(type) ->                                                │
│    importlib("connectors.{type}.adapter")                                   │
└─────────────────────────────────────────────────────────────────────────────┘
         │
         ▼  Parquet files
┌─────────────────────────────────────────────────────────────────────────────┐
│  CORE ENGINE  (src/)                                                        │
│                                                                             │
│  ┌─────────────────────┐  ┌──────────────────┐  ┌──────────────────────┐  │
│  │ data_sync.py         │  │ config.py         │  │ profiler.py          │  │
│  │  DataSource ABC      │  │  data_description │  │  Parquet -> stats    │  │
│  │  SyncState (JSON)    │  │  .md parser       │  │  alerts, sampling    │  │
│  │  DataSyncManager     │  │  TableConfig      │  │  -> profiles.json    │  │
│  │  create_data_source()│  │  WhereFilter      │  └──────────────────────┘  │
│  └─────────────────────┘  │  ForeignKey        │                            │
│                            │  get_config()      │  ┌──────────────────────┐  │
│                            └──────────────────┘  │ parquet_manager.py    │  │
│                                                   │  CSV->Parquet, merge  │  │
│                                                   │  upsert, schema       │  │
│                                                   └──────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────┘
         │
         │  /data/src_data/parquet/
         ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  AUTH PROVIDERS  (auth/)                    auto-discovery via scan         │
│                                                                             │
│  ┌────────────────┐  ┌────────────────┐  ┌──────────────────────┐         │
│  │ auth/google/    │  │ auth/password/ │  │ auth/desktop/         │        │
│  │                 │  │                │  │                       │        │
│  │ Google OAuth    │  │ Email+password │  │ JWT for desktop app   │        │
│  │ SSO (Authlib)   │  │ Argon2 hash   │  │ visible=False         │        │
│  │ domain restrict │  │ SendGrid email │  │ (API-only, not login) │        │
│  │ order=10        │  │ order=20       │  │ order=100             │        │
│  └────────────────┘  └────────────────┘  └──────────────────────┘         │
│                                                                             │
│  ABC: AuthProvider (get_name, get_blueprint, get_login_button, is_avail.)  │
│  Discovery: discover_providers() -> scans auth/*/provider.py               │
│  Contract: all providers set session["user"] = {email, name, picture}      │
└─────────────────────────────────────────────────────────────────────────────┘
         │
         │  Blueprints registered in Flask app
         ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  WEB PORTAL  (webapp/)                                                      │
│                                                                             │
│  ┌───────────────────┐  ┌──────────────────────────────────────────────┐   │
│  │ app.py (Flask)     │  │  Pages                                      │   │
│  │ - discover auth    │  │  /dashboard     - account, stats, setup     │   │
│  │   providers        │  │  /catalog       - data catalog + profiles   │   │
│  │ - register         │  │  /corporate-memory - knowledge + voting     │   │
│  │   blueprints       │  │  /activity-center - intelligence overview   │   │
│  │ - inject_config()  │  └──────────────────────────────────────────────┘   │
│  │ - routes           │                                                     │
│  └───────────────────┘  ┌──────────────────────────────────────────────┐   │
│                          │  API Endpoints                               │   │
│  ┌───────────────────┐  │  /webhooks/jira     (HMAC, -> jira connector)│   │
│  │ webapp services    │  │  /api/telegram/*    (link/unlink/status)     │   │
│  │ user_service       │  │  /api/desktop/*     (JWT, scripts, run)     │   │
│  │ account_service    │  │  /api/sync-settings (GET/POST)              │   │
│  │ sync_settings_svc  │  │  /api/corporate-memory/* (CRUD, votes)      │   │
│  │ telegram_service   │  │  /api/catalog/profile/<table>               │   │
│  │ email_service      │  │  /health            (service health)        │   │
│  │ health_service     │  └──────────────────────────────────────────────┘   │
│  │ corporate_memory   │                                                     │
│  └───────────────────┘  Config chain: instance.yaml -> loader -> Config -> │
│                          inject_config() -> {{ config.X }} in Jinja        │
└─────────────────────────────────────────────────────────────────────────────┘
         │
         │
         ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  BACKGROUND SERVICES  (services/)        each = __main__.py + systemd      │
│                                                                             │
│  ┌────────────────────────┐  ┌─────────────────────────────────────────┐   │
│  │ services/telegram_bot/  │  │ services/ws_gateway/                    │   │
│  │                         │  │                                         │   │
│  │ bot.py     polling +    │  │ gateway.py   WebSocket TCP:8765        │   │
│  │            HTTP socket  │  │              + HTTP dispatch socket     │   │
│  │ runner.py  script exec  │  │ auth.py      JWT validation            │   │
│  │ sender.py  msg dispatch │  │ config.py    gateway config            │   │
│  │ dispatch.py -> WS gw   │  │                                         │   │
│  │ storage.py  JSON state  │  │ Heartbeat: ping/pong, 3 miss = drop   │   │
│  │ status.py  /status cmd  │  │ Per-user connection limit (5)          │   │
│  │                         │  │                                         │   │
│  │ Always running (systemd)│  │ Always running (systemd)               │   │
│  └────────────────────────┘  └─────────────────────────────────────────┘   │
│                                                                             │
│  ┌────────────────────────┐  ┌─────────────────────────────────────────┐   │
│  │ services/               │  │ services/                               │   │
│  │   corporate_memory/     │  │   session_collector/                    │   │
│  │                         │  │                                         │   │
│  │ collector.py            │  │ collector.py                            │   │
│  │  Scans CLAUDE.local.md  │  │  Copies .jsonl from user homes         │   │
│  │  -> Claude Haiku -> JSON│  │  to /data/user_sessions/               │   │
│  │  MD5 change detection   │  │  Idempotent, atomic writes             │   │
│  │ prompts.py              │  │                                         │   │
│  │  LLM prompts for        │  │ Timer: every 6 hours                   │   │
│  │  knowledge extraction   │  │                                         │   │
│  │                         │  │                                         │   │
│  │ Timer: every 30 min     │  │                                         │   │
│  └────────────────────────┘  └─────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
         │
         │  Unix sockets + /data/ filesystem
         ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  SERVER INFRASTRUCTURE  (server/)                                           │
│                                                                             │
│  ┌──────────────────┐  ┌────────────────────┐  ┌───────────────────────┐  │
│  │ Deployment        │  │ User Management     │  │ Web Server            │  │
│  │ setup.sh          │  │ bin/add-analyst     │  │ webapp-nginx.conf     │  │
│  │ deploy.sh (CI/CD) │  │ bin/list-analysts   │  │ webapp.service        │  │
│  │ webapp-setup.sh   │  │ bin/notify-runner   │  │ SSL (Let's Encrypt)   │  │
│  │ sudoers rules     │  │ bin/notify-scripts  │  │ Gunicorn + Unix sock  │  │
│  └──────────────────┘  └────────────────────┘  └───────────────────────┘  │
│                                                                             │
│  Groups: dataread (analysts) | data-private (privileged) | data-ops (admin) │
│                                                                             │
│  /data/                                                                     │
│  ├── src_data/parquet/          shared data (readonly for analysts)         │
│  ├── src_data/metadata/         sync_state.json, profiles.json             │
│  ├── src_data/raw/jira/         webhook JSON, attachments                  │
│  ├── docs/ , scripts/           documentation, helper scripts              │
│  ├── notifications/             telegram_users, desktop_users, codes       │
│  ├── corporate-memory/          knowledge.json, votes.json                 │
│  └── user_sessions/             centralized Claude Code transcripts        │
└─────────────────────────────────────────────────────────────────────────────┘
         │
         │  rsync (SSH) - scripts/sync_data.sh (bi-directional)
         ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  ANALYST WORKSTATION  (local)                                               │
│                                                                             │
│  server/   (read-only, rsynced from broker)                                │
│  ├── parquet/, docs/, scripts/, metadata/                                  │
│                                                                             │
│  user/     (writable workspace, backed up to server)                       │
│  ├── duckdb/analytics.duckdb    SQL views over parquet                     │
│  ├── notifications/*.py         custom notification scripts                │
│  ├── sessions/                  Claude Code transcripts                    │
│  └── artifacts/                 analysis outputs                           │
│                                                                             │
│  .claude/rules/                 corporate memory knowledge rules           │
│                                                                             │
│  Claude Code <- local analysis over DuckDB + Parquet                       │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Auto-Discovery Patterns

The platform uses three symmetrical auto-discovery mechanisms. Adding a new
connector, auth method, or service requires no changes to existing code.

### 1. Connector Discovery (`src/data_sync.py`)

```
config/instance.yaml -> data_source.type: "keboola"
  -> importlib.import_module("connectors.keboola.adapter")
  -> KeboolaDataSource (implements DataSource ABC)
```

- Factory: `create_data_source(type)` in `src/data_sync.py`
- Connectors live in `connectors/{name}/adapter.py`
- Must export a `DataSource` subclass or a `create_data_source()` factory function
- Keboola is hard-coded for ImportError handling; all others use dynamic import

### 2. Auth Provider Discovery (`auth/__init__.py`)

```
startup -> scan auth/*/provider.py
  -> import `provider` instance
  -> filter by is_available() (checks env vars)
  -> register blueprint + login button in Flask
```

- ABC: `AuthProvider` with methods `get_name()`, `get_blueprint()`, `get_login_button()`, `is_available()`, `init_app()`
- Session contract: all providers set `session["user"] = {email, name, picture}`
- Login page renders buttons dynamically, sorted by `order` field

### 3. Service Pattern (`services/*/__main__.py`)

```
python -m services.<name>       # entry point
services/<name>/systemd/        # unit files
deploy.sh auto-discovers        # systemd/* in each service dir
```

- Each service is self-contained: code, systemd units, and config in one directory
- `deploy.sh` scans `services/*/systemd/*.service` and `connectors/*/systemd/*.service`
- Long-running services (telegram_bot, ws_gateway) use async dual-server model
- Periodic services (corporate_memory, session_collector) are systemd timer oneshots

## Data Flows

### Pull Sync (Keboola)

```
Keboola Storage API
  -> connectors/keboola/client.py  (export CSV with filters)
  -> src/parquet_manager.py        (convert to typed Parquet)
  -> /data/src_data/parquet/       (stored on broker)
  -> rsync to analyst              (scripts/sync_data.sh)
  -> DuckDB views                  (scripts/setup_views.sh)
```

Sync strategies: `full_refresh`, `incremental`, `partitioned`, `chunked_initial_load`.

### Push Sync (Jira)

```
Jira Cloud webhook (issue created/updated/deleted)
  -> connectors/jira/webhook.py        (HMAC-SHA256 verification)
  -> connectors/jira/service.py        (fetch full issue + attachments)
  -> /data/src_data/raw/jira/issues/   (atomic JSON write)
  -> connectors/jira/incremental_transform.py (update monthly Parquet)
  -> /data/src_data/parquet/jira/      (6 tables: issues, comments,
                                         attachments, changelog,
                                         issuelinks, remote_links)
```

Background jobs supplement the webhook pipeline:
- `jira-sla-poll` (every 5 min): refreshes SLA fields for open tickets
- `jira-consistency` (every 6h): detects and backfills missing issues

### Notification Pipeline

```
~/user/notifications/*.py             analyst's custom scripts
  -> server/bin/notify-runner         (cron, executes with timeout)
  -> cooldown check                   (~/.notifications/state/)
  ├-> services/telegram_bot/          (Unix socket /run/notify-bot/bot.sock)
  │     -> Telegram chat message (text or photo)
  └-> services/ws_gateway/            (Unix socket /run/ws-gateway/ws.sock)
        -> WebSocket push to desktop app
```

Script output format:
```json
{
  "notify": true,
  "title": "Revenue dropped 25%",
  "message": "Details...",
  "cooldown": "6h",
  "image_path": "/tmp/chart.png"
}
```

### Knowledge Loop (Corporate Memory)

```
Analyst writes CLAUDE.local.md        (insights, patterns, tips)
  -> scripts/sync_data.sh             (uploads to server)
  -> services/corporate_memory/       (timer, every 30 min)
     -> MD5 change detection
     -> Claude Haiku extracts knowledge items
     -> /data/corporate-memory/knowledge.json
  -> webapp /corporate-memory          (voting UI: upvote/downvote)
  -> scripts/sync_data.sh             (downloads to analyst)
  -> .claude/rules/                   (rules for Claude Code)
  -> Claude Code uses rules in next session
```

## Module Reference

### Core Engine (`src/`)

| File | Lines | Responsibility |
|------|-------|----------------|
| `data_sync.py` | ~1400 | `DataSource` ABC, `SyncState`, `DataSyncManager`, connector factory |
| `config.py` | ~600 | Parse `data_description.md` YAML blocks, `TableConfig`, `WhereFilter`, `ForeignKey` |
| `parquet_manager.py` | ~750 | CSV-to-Parquet conversion, merge, upsert, schema enforcement |
| `profiler.py` | ~1200 | Data profiling: stats, alerts, type classification -> `profiles.json` |

### Connectors (`connectors/`)

| Module | Files | Sync Model | Description |
|--------|-------|------------|-------------|
| `keboola/` | adapter.py, client.py, tests/ | Pull (DataSource ABC) | Keboola Storage API, type mapping, metadata caching (24h TTL) |
| `jira/` | webhook.py, service.py, transform.py, incremental_transform.py, file_lock.py, scripts/, systemd/, tests/ | Push (webhook) | Real-time webhook pipeline, SLA polling, consistency monitoring, 6 output Parquet tables |

### Auth Providers (`auth/`)

| Provider | Available when | Login UI | Order | Description |
|----------|---------------|----------|-------|-------------|
| `google/` | `GOOGLE_CLIENT_ID` set | Yes | 10 | Google OAuth SSO with domain restriction |
| `password/` | `SENDGRID_API_KEY` set | Yes | 20 | Email + password for external users (Argon2, rate limiting) |
| `desktop/` | `DESKTOP_JWT_SECRET` set | No (API-only) | 100 | JWT tokens for native desktop app |

### Background Services (`services/`)

| Service | Type | Schedule | Description |
|---------|------|----------|-------------|
| `telegram_bot/` | Long-running | Always on | Telegram polling + HTTP dispatch socket, script execution, /status /test commands |
| `ws_gateway/` | Long-running | Always on | WebSocket TCP:8765 + HTTP dispatch socket, JWT auth, heartbeat |
| `corporate_memory/` | Timer oneshot | Every 30 min | AI knowledge extraction from CLAUDE.local.md via Claude Haiku |
| `session_collector/` | Timer oneshot | Every 6 hours | Copy session .jsonl from user homes to central storage |

### Web Portal (`webapp/`)

| File | Responsibility |
|------|----------------|
| `app.py` | Flask factory, blueprint registration, route definitions, context processors |
| `config.py` | Load `instance.yaml`, expose `Config` to templates |
| `auth.py` | Core auth infrastructure: `login_required`, `validate_email_domain`, `/login`, `/logout` |
| `user_service.py` | Username derivation, SSH key validation, system account creation |
| `account_service.py` | Dashboard account widget data, cron info, sync status |
| `sync_settings_service.py` | Per-user dataset sync preferences |
| `telegram_service.py` | Telegram account linking/unlinking |
| `desktop_auth.py` | JWT generation/validation, desktop app link state |
| `password_auth.py` | Password auth implementation (Argon2, rate limiting, token workflow) |
| `email_service.py` | SendGrid integration for setup/reset emails |
| `corporate_memory_service.py` | Knowledge CRUD, voting, user rules regeneration |
| `health_service.py` | System health checks (services, timers, disk, load, webhooks) |
| `notification_images.py` | Serve chart PNGs generated by notification runner |
| `utils/metric_parser.py` | Parse business metric YAML definitions for catalog UI |

### Server Infrastructure (`server/`)

| File | Responsibility |
|------|----------------|
| `setup.sh` | Initial server bootstrap (groups, users, directories, venv) |
| `deploy.sh` | CI/CD deployment (git pull, deps, scripts, services, ACLs) |
| `webapp-setup.sh` | Nginx + SSL + Gunicorn setup |
| `webapp-nginx.conf` | Nginx reverse proxy config (HTTPS, WebSocket upgrade) |
| `webapp.service` | Systemd unit for Gunicorn |
| `sudoers-deploy` | Sudo rules for deploy user (least-privilege) |
| `sudoers-webapp` | Sudo rules for www-data |
| `bin/add-analyst` | Create analyst user with workspace structure |
| `bin/list-analysts` | List registered analysts |
| `bin/notify-runner` | Execute user notification scripts, dispatch to bot + gateway |
| `bin/notify-scripts` | List/run notification scripts for a user |

### Analyst Scripts (`scripts/`)

| File | Responsibility |
|------|----------------|
| `sync_data.sh` | Bi-directional rsync: download data, upload workspace, refresh DuckDB |
| `setup_views.sh` | Create/replace DuckDB views over all Parquet files |
| `duckdb_manager.py` | DuckDB setup utility |
| `dev_run.py` | Development server with auth bypass |
| `collect_session.py` | Session transcript collector (used by service) |
| `generate_user_sync_configs.py` | Generate per-user sync config files |

## Analyst Workspace Layout

Created by `server/bin/add-analyst` for each registered user:

```
/home/{username}/
├── server/                    read-only symlinks to shared data
│   ├── parquet/               -> /data/src_data/parquet
│   ├── docs/                  -> /data/docs
│   ├── scripts/               -> /data/scripts
│   ├── metadata/              -> /data/src_data/metadata
│   └── jira_attachments/      -> /data/src_data/raw/jira/attachments
├── user/                      writable workspace (backed up to server)
│   ├── duckdb/                local DuckDB database
│   ├── notifications/         custom notification scripts (*.py)
│   ├── artifacts/             analysis outputs
│   ├── scripts/               user helper scripts
│   ├── parquet/               user Parquet files
│   └── sessions/              Claude Code session transcripts
├── .notifications/            notification runner state
│   ├── state/                 cooldown tracking (JSON per script)
│   └── logs/                  runner logs
└── .claude/
    └── rules/                 corporate memory knowledge rules (auto-synced)
```

## Security Model

### System Groups

| Group | Access |
|-------|--------|
| `data-ops` | Full admin access to all server resources |
| `dataread` | Read access to public Parquet data |
| `data-private` | Read access to sensitive/restricted data |

### Authentication Layers

| Layer | Mechanism | Scope |
|-------|-----------|-------|
| Web portal | Google OAuth / email+password | Browser sessions |
| Desktop app | JWT Bearer tokens | API endpoints (`/api/desktop/*`) |
| Jira webhook | HMAC-SHA256 signature | Webhook endpoint |
| SSH access | Key-based auth only | Data sync (rsync) |
| Inter-service | Unix socket permissions | Bot, gateway, webapp |

### Permission Boundaries

- Analysts cannot access other users' home directories
- Webapp (www-data) uses sudoers-whitelisted commands for user operations
- Deploy user has explicit sudo rules for service management
- Staging directory (`/tmp/data_analyst_staging`) uses setgid for group ownership
- All JSON state files written atomically: `tempfile.mkstemp()` + `os.fchmod()` + `os.replace()`

## Configuration Chain

```
config/instance.yaml                     (instance-specific, not committed)
    | loaded by config/loader.py
    | ${ENV_VAR} references resolved from .env / environment
    v
webapp/config.py                         (Flask Config class)
    | _load_instance_config() at module level
    | _get(config, *keys) for safe nested access
    v
inject_config() context processor       (exposes Config to templates)
    v
{{ config.INSTANCE_NAME }} in Jinja2    (all templates have access)
```

Validation: `config/loader.py` checks required fields at startup (`instance.name`,
`auth.allowed_domain`, `server.host`, `server.hostname`, `auth.webapp_secret_key`).
Missing required fields cause immediate startup failure with a clear error message.

## Server Filesystem Layout

```
/opt/data-analyst/
├── repo/                    git repository (deployed via CI/CD)
├── .venv/                   Python virtual environment
├── logs/                    application logs
└── .env                     secrets (mode 0640)

/data/
├── src_data/
│   ├── parquet/             shared Parquet files (readonly for analysts)
│   ├── metadata/            sync_state.json, profiles.json, table_metadata.json
│   └── raw/jira/            webhook JSON files, attachments
├── docs/                    documentation and schema
├── scripts/                 helper scripts synced to analysts
├── notifications/           telegram_users.json, desktop_users.json, pending_codes.json
├── corporate-memory/        knowledge.json, votes.json, user_hashes.json
└── user_sessions/           centralized Claude Code session transcripts

/run/
├── notify-bot/bot.sock      Telegram bot HTTP socket
├── ws-gateway/ws.sock       WebSocket gateway HTTP socket
└── webapp/webapp.sock       Gunicorn WSGI socket
```

## CI/CD

### Deploy Guard (`.github/workflows/deploy-guard.yml`)

Runs on every pull request:
1. `pytest tests/test_deploy_guard.py` - validates deploy.sh/sudoers/systemd consistency
2. `pytest tests/test_sync_data.py -m "not live"` - validates sync script reliability
3. `visudo -cf server/sudoers-*` - validates sudoers syntax in Docker

### Deployment (`.github/workflows/deploy.yml.example`)

Runs on push to main (or manual trigger):
1. SSH into server
2. Execute `server/deploy.sh` (git pull, deps, scripts, services, ACLs)
