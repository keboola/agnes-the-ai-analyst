# AI Data Analyst — Refactoring Design Spec

**Date:** 2026-03-27
**Status:** Draft
**Target:** Greenfield demo with Keboola internal data

## 1. Problem Statement

The platform was built iteratively as an internal tool and needs to become a product for external customers (Groupon, others). Key problems:

1. **Fragile filesystem state** — 10+ JSON files, permission conflicts between processes (www-data, deploy, root, user) cause outages
2. **No API** — all operations via SSH + bash scripts, no programmatic control
3. **Security via Linux groups** — no real RBAC, SSH keys visible in `ps aux`, root reads user homes
4. **Complex installation** — 10+ manual steps, specific OS requirements, dual-repo pattern with symlinks
5. **Operations nightmare** — scattered scripts, no unified logging/monitoring, creator calls it "duct tape solution"

The system is designed for AI agents — humans discuss with AI, AI handles everything (user, admin, dev operations).

**Constraint:** UX must remain identical. Web catalog, data sync, offline Claude Code analysis, Telegram notifications, corporate memory — all preserved.

## 2. Architecture

### Target State

```
SERVER (Docker + Kamal):
┌──────────────────────────────────────────────────┐
│          FastAPI Main App (1 process)             │
│  ├── Web UI (Jinja2 templates)                   │
│  ├── REST API (/api/*)                           │
│  ├── WebSocket (/ws/notifications)               │
│  └── Auth (JWT + pluggable providers)            │
└──────────────────────────────────────────────────┘
┌─────────────────┐  ┌──────────────────────────────┐
│ Scheduler sidecar│  │ Telegram bot (optional)      │
│ Calls /api/      │  │ Long-running daemon          │
└─────────────────┘  └──────────────────────────────┘

/data/state/system.duckdb     ← system state (users, sync, knowledge, audit)
/data/analytics/server.duckdb ← views on parquet files
/data/parquet/**              ← data files

LOCAL (analyst):
┌──────────────────────────────────────────────────┐
│  da CLI (uv tool install data-analyst-cli)       │
│  user/duckdb/analytics.duckdb ← views + user tbls│
│  server/parquet/** ← downloaded via da sync      │
│  Claude Code ← works offline with DuckDB         │
└──────────────────────────────────────────────────┘
```

### Key Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Web framework | FastAPI only (no Flask) | One framework, OpenAPI auto-schema, async native, Jinja2 support |
| State storage | DuckDB | Already in stack, agent can join state with analytics, better than SQLite for analytical queries |
| CLI tool | `da` via `uv tool install` | AI-agent native interface, no Docker dependency locally |
| Server deploy | Docker + Kamal | Zero-downtime deploys, auto-SSL, simple config |
| Architecture | Hybrid (main app + scheduler sidecar + optional telegram) | 3 containers max, WebSocket in main app |
| Auth providers | All 3 (Google OAuth + Email magic link + Password) | Full compatibility with existing users |
| LLM provider | Configurable in instance.yaml | User chooses: local Ollama, Anthropic, OpenAI, AI Gateway |
| Python tooling | uv everywhere (no pip) | Faster, deterministic, modern |

## 3. Data Layer

### Server DuckDB: system.duckdb

```sql
-- Users & RBAC
CREATE TABLE users (
  id VARCHAR PRIMARY KEY,
  email VARCHAR UNIQUE NOT NULL,
  name VARCHAR,
  role VARCHAR DEFAULT 'analyst',  -- viewer, analyst, admin, km_admin
  password_hash VARCHAR,
  setup_token VARCHAR,
  reset_token VARCHAR,
  created_at TIMESTAMP DEFAULT current_timestamp,
  updated_at TIMESTAMP
);

CREATE TABLE user_sync_settings (
  user_id VARCHAR REFERENCES users(id),
  dataset VARCHAR NOT NULL,
  enabled BOOLEAN DEFAULT false,
  table_mode VARCHAR DEFAULT 'all',  -- all, explicit
  tables JSON,
  updated_at TIMESTAMP,
  PRIMARY KEY (user_id, dataset)
);

CREATE TABLE dataset_permissions (
  user_id VARCHAR REFERENCES users(id),
  dataset VARCHAR NOT NULL,
  access VARCHAR DEFAULT 'read',  -- read, none
  PRIMARY KEY (user_id, dataset)
);

-- Sync state + history
CREATE TABLE sync_state (
  table_id VARCHAR PRIMARY KEY,
  last_sync TIMESTAMP,
  rows BIGINT,
  file_size_bytes BIGINT,
  uncompressed_size_bytes BIGINT,
  columns INTEGER,
  hash VARCHAR,
  status VARCHAR DEFAULT 'ok',
  error TEXT
);

CREATE TABLE sync_history (
  id VARCHAR PRIMARY KEY,
  table_id VARCHAR NOT NULL,
  synced_at TIMESTAMP NOT NULL,
  rows BIGINT,
  duration_ms INTEGER,
  status VARCHAR,
  error TEXT
);

-- Corporate memory
CREATE TABLE knowledge_items (
  id VARCHAR PRIMARY KEY,
  title VARCHAR NOT NULL,
  content TEXT,
  category VARCHAR,
  tags TEXT[],
  status VARCHAR DEFAULT 'pending',  -- pending, approved, mandatory, rejected
  contributors TEXT[],
  source_user VARCHAR,
  audience VARCHAR,
  created_at TIMESTAMP,
  updated_at TIMESTAMP
);

CREATE TABLE knowledge_votes (
  item_id VARCHAR REFERENCES knowledge_items(id),
  user_id VARCHAR REFERENCES users(id),
  vote INTEGER,  -- 1 or -1
  voted_at TIMESTAMP,
  PRIMARY KEY (item_id, user_id)
);

-- Audit
CREATE TABLE audit_log (
  id VARCHAR PRIMARY KEY,
  timestamp TIMESTAMP NOT NULL,
  user_id VARCHAR,
  action VARCHAR NOT NULL,
  resource VARCHAR,
  params JSON,
  result VARCHAR,
  duration_ms INTEGER
);

-- Notifications
CREATE TABLE telegram_links (
  user_id VARCHAR PRIMARY KEY REFERENCES users(id),
  chat_id BIGINT NOT NULL,
  linked_at TIMESTAMP
);

CREATE TABLE pending_codes (
  code VARCHAR PRIMARY KEY,
  chat_id BIGINT NOT NULL,
  created_at TIMESTAMP
);

CREATE TABLE script_registry (
  id VARCHAR PRIMARY KEY,
  name VARCHAR NOT NULL,
  owner VARCHAR REFERENCES users(id),
  schedule VARCHAR,  -- cron expression or null
  source TEXT NOT NULL,
  deployed_at TIMESTAMP,
  last_run TIMESTAMP,
  last_status VARCHAR
);

-- Table registry
CREATE TABLE table_registry (
  id VARCHAR PRIMARY KEY,
  name VARCHAR NOT NULL,
  folder VARCHAR,
  sync_strategy VARCHAR,
  primary_key VARCHAR,
  description TEXT,
  registered_by VARCHAR,
  registered_at TIMESTAMP
);

-- Profiles
CREATE TABLE table_profiles (
  table_id VARCHAR PRIMARY KEY,
  profile JSON NOT NULL,
  profiled_at TIMESTAMP
);
```

### Server DuckDB: server.duckdb

Auto-generated views on parquet files:
```sql
CREATE VIEW orders AS SELECT * FROM read_parquet('/data/parquet/sales/orders.parquet');
CREATE VIEW customers AS SELECT * FROM read_parquet('/data/parquet/sales/customers.parquet');
-- Generated from schema.yml by profiler/sync
```

### Local DuckDB: analytics.duckdb

Views on local parquets (generated by `da sync`):
```sql
CREATE VIEW orders AS SELECT * FROM read_parquet('./server/parquet/sales/orders.parquet');
-- User-created tables survive da sync (rebuild drops only views, not tables)
```

### Repository Pattern

```
src/repositories/
  __init__.py          # get_system_db(), get_analytics_db() factories
  users.py             # UserRepository (CRUD + role checks)
  sync_state.py        # SyncStateRepository (state + history)
  knowledge.py         # KnowledgeRepository (items + votes + governance)
  audit.py             # AuditRepository (append + query)
  scripts.py           # ScriptRepository (registry + scheduling)
  table_registry.py    # TableRegistryRepository
  notifications.py     # TelegramRepository + PendingCodeRepository
```

## 4. API Endpoints

### FastAPI Router Structure

```
app/
  main.py                 # FastAPI app, lifespan events, middleware
  auth/
    router.py             # POST /auth/login, /auth/token, /auth/logout
    jwt.py                # JWT create/verify (PyJWT)
    providers/            # Pluggable: google/, email/, password/
    dependencies.py       # get_current_user, require_role(Role)
  web/
    router.py             # Web UI: GET /, /catalog, /memory, /settings...
    templates/            # Jinja2 (migrated from webapp/templates/)
    static/               # CSS, JS, images
  api/
    sync.py               # GET /api/sync/manifest, POST /api/sync/trigger
    data.py               # GET /api/data/{table}/download
    query.py              # POST /api/query
    scripts.py            # GET/POST /api/scripts, POST /api/scripts/{id}/run
    users.py              # CRUD /api/users
    settings.py           # GET/PUT /api/users/{id}/settings
    memory.py             # CRUD /api/memory, POST /api/memory/{id}/vote
    health.py             # GET /api/health
    upload.py             # POST /api/upload/sessions, /artifacts, /local-md
  ws/
    notifications.py      # WebSocket /ws/notifications
```

### Key Endpoints

| Endpoint | Method | Auth | Purpose |
|----------|--------|------|---------|
| `/api/sync/manifest` | GET | JWT (analyst+) | Hash-based manifest of all synced data |
| `/api/sync/trigger` | POST | JWT (admin) | Trigger data sync from source |
| `/api/data/{table}/download` | GET | JWT (analyst+) | Stream parquet file (ETag support) |
| `/api/query` | POST | JWT (analyst+) | Execute SQL against server DuckDB |
| `/api/scripts` | GET/POST | JWT (analyst+) | List/deploy user scripts |
| `/api/scripts/{id}/run` | POST | JWT (analyst+) | Execute script in sandbox |
| `/api/users` | GET/POST/DELETE | JWT (admin) | User management |
| `/api/memory` | GET/POST/PUT | JWT (analyst+) | Corporate memory CRUD |
| `/api/health` | GET | none | Structured health check |
| `/api/upload/sessions` | POST | JWT (analyst+) | Upload Claude session transcripts |
| `/api/upload/local-md` | POST | JWT (analyst+) | Upload CLAUDE.local.md content |

### Sync Protocol

1. CLI calls `GET /api/sync/manifest` → receives hashes per table/asset
2. CLI compares with local `~/.config/da/sync_state.json`
3. For each changed table: `GET /api/data/{table}/download` → streaming to `./server/parquet/`
4. Download changed docs, rules, profiles, scripts
5. Upload new sessions, artifacts, CLAUDE.local.md content
6. Rebuild local DuckDB views (preserve user-created tables)
7. Update local sync manifest

## 5. CLI Tool (`da`)

### Structure

```
cli/
  main.py             # Typer app, --server/--json global options
  config.py           # ~/.config/da/ management (token, server URL, sync state)
  client.py           # httpx async client (JWT auth, retry, streaming, progress bars)
  duckdb_local.py     # Local DuckDB management (create views, query, explore)
  commands/
    auth.py           # da login/logout/whoami
    sync.py           # da sync [--table X] [--upload-only] [--docs-only]
    query.py          # da query "SQL" [--remote] [--json] [--format csv/table/json]
    scripts.py        # da scripts list/run/deploy/undeploy
    explore.py        # da explore {table}
    admin.py          # da admin add-user/remove-user/list-users/set-role
    status.py         # da status [--local] [--json]
    server.py         # da server deploy/rollback/logs/status/backup
    setup.py          # da setup init/test-connection/deploy/first-sync/verify
    diagnose.py       # da diagnose [--symptom X] [--component Y]
    skills.py         # da skills list/show
    infra.py          # da infra provision/status/deploy (future)
  skills/             # Markdown knowledge base for AI agents
    setup.md
    troubleshoot.md
    connectors.md
    notifications.md
    corporate-memory.md
    security.md
    backup-restore.md
    upgrade.md
```

### Distribution

```toml
[project]
name = "data-analyst-cli"
requires-python = ">=3.11"
dependencies = ["typer>=0.12", "httpx>=0.27", "duckdb>=1.1", "rich>=13", "pyjwt>=2.8"]

[project.scripts]
da = "cli.main:app"
```

Install: `uv tool install data-analyst-cli`

### Offline Capability

After `da sync`, everything works without network:
- `da query` → local DuckDB
- `da scripts run` → local Python execution
- `da explore` → local profile data
- `da status --local` → sync timestamps from local manifest

## 6. Deploy & Infrastructure

### Docker

```dockerfile
FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY . .
CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### Docker Compose (dev)

```yaml
services:
  app:
    build: .
    ports: ["8000:8000"]
    volumes: [".:/app", "data:/data"]
    env_file: .env
    command: uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

  scheduler:
    build: .
    volumes: ["data:/data"]
    env_file: .env
    command: uv run python -m services.scheduler

  telegram-bot:
    build: .
    volumes: ["data:/data"]
    env_file: .env
    command: uv run python -m services.telegram_bot
    profiles: ["full"]

volumes:
  data:
```

### Scheduler Sidecar

The scheduler is a lightweight process that triggers jobs by calling the main app's API:

```python
# services/scheduler/__main__.py
import httpx
from apscheduler.schedulers.blocking import BlockingScheduler

API_URL = os.environ.get("API_URL", "http://app:8000")
API_TOKEN = os.environ.get("SCHEDULER_API_TOKEN")  # internal service token

scheduler = BlockingScheduler()

@scheduler.scheduled_job("interval", minutes=15)
def data_refresh():
    httpx.post(f"{API_URL}/api/sync/trigger", headers={"Authorization": f"Bearer {API_TOKEN}"})

@scheduler.scheduled_job("interval", minutes=30)
def corporate_memory():
    httpx.post(f"{API_URL}/api/internal/collect-knowledge", headers={"Authorization": f"Bearer {API_TOKEN}"})

# ... more jobs
scheduler.start()
```

This keeps all business logic in the main app. The scheduler is stateless and restartable.

### Kamal (production)

- Auto-SSL via Kamal Proxy (Let's Encrypt)
- Zero-downtime deploy
- Healthcheck on `/api/health`
- Staging: `kamal deploy -d staging`
- Production: `kamal deploy`
- Rollback: `kamal rollback`

### CI/CD (GitHub Actions)

```
push → pytest (unit) → docker compose test (integration) → build+push GHCR
PR → kamal deploy staging
merge main → kamal deploy production
```

## 7. Security

### RBAC

| Role | Permissions |
|------|-------------|
| `viewer` | Read catalog, view profiles, browse corporate memory |
| `analyst` | + sync data, run queries, vote on knowledge, run/deploy scripts |
| `admin` | + manage users, approve knowledge, trigger sync, view audit |
| `km_admin` | + corporate memory governance (approve/reject/mandate) |

Dataset-level permissions restrict which datasets each user can access.

### Auth Flow

1. Web: user logs in via Google OAuth / Email magic link / Password
2. Server issues JWT (contains: user_id, email, role, exp)
3. CLI: `da login` → OAuth browser flow → JWT stored in `~/.config/da/token.json`
4. All API calls include JWT in Authorization header
5. FastAPI dependency validates JWT + checks role permissions

### Audit Trail

Every API call logged to `audit_log` table:
- timestamp, user_id, action, resource, params, result, duration_ms
- Queryable by agent: `da query "SELECT * FROM system.audit_log WHERE ..."`

### Script Sandboxing

User scripts run in isolated Docker container:
- Read-only DuckDB access
- Memory limit: 512MB, time limit: 5min
- No network (except notification dispatch)
- Whitelisted Python packages: pandas, duckdb, matplotlib, numpy

## 8. Testing Strategy

```
tests/
  unit/                   # No I/O, mocked dependencies
    test_repositories.py  # In-memory DuckDB
    test_sync_logic.py
    test_auth.py
    test_rbac.py
  integration/            # Docker compose, real DuckDB + sample data
    test_api_endpoints.py
    test_sync_flow.py
    test_cli_commands.py
  fixtures/
    sample_data/          # Small parquets for testing
    instance.yaml         # Test config
```

## 9. Migration Path

1. **Greenfield demo** — build new system from scratch with sample Keboola data
2. **Validate** — end-to-end: setup → sync → query → scripts → notifications
3. **Migrate internal** — point new system at Keboola internal, migrate users
4. **Migrate Groupon** — deploy new system for Groupon with their config
5. **Deprecate old** — remove old server infrastructure

## 10. Reused Code

| File | Status | Notes |
|------|--------|-------|
| `src/config.py` | Reused as-is | TableConfig, Config parsing |
| `src/parquet_manager.py` | Reused as-is | Parquet conversion |
| `connectors/keboola/` | Reused as-is | Keboola adapter + client |
| `connectors/bigquery/` | Reused as-is | BigQuery adapter + client |
| `connectors/jira/` | Reused as-is | Jira connector |
| `connectors/llm/` | Reused as-is | LLM abstraction |
| `connectors/openmetadata/` | Reused as-is | Catalog enrichment |
| `src/data_sync.py` | Rewired | SyncState → DuckDB repository |
| `src/remote_query.py` | Wrapped | Query logic wrapped by API endpoint |
| `src/profiler.py` | Rewired | Output to DuckDB instead of JSON |
| `src/table_registry.py` | Rewired | JSON → DuckDB repository |
| `webapp/corporate_memory_service.py` | Rewired | Business logic preserved, I/O swapped |
| `webapp/templates/` | Migrated | Jinja2 templates work in FastAPI |
| `auth/` | Migrated | Provider pattern preserved |

## 11. Deleted Code

| File | Reason |
|------|--------|
| `server/setup.sh` | Replaced by Docker |
| `server/webapp-setup.sh` | Replaced by Docker + Kamal |
| `server/deploy.sh` | Replaced by Kamal |
| `server/sudoers-*` | No more Linux user management |
| `server/bin/add-analyst` | Replaced by API + CLI |
| `scripts/sync_data.sh` | Replaced by `da sync` |
| `services/*/systemd/` | Replaced by Docker Compose |
| `webapp/user_service.py` | Rewritten for DB-based users |
| `webapp/sync_settings_service.py` (sudo parts) | Replaced by API |
