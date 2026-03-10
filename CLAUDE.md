# AI Data Analyst

Open-source data distribution platform for AI analytical systems. Syncs data from various sources, converts to Parquet, and distributes to analysts who use Claude Code for local analysis.

## First-Time Setup

When a user opens this project for the first time, guide them through interactive setup:

### Step 1: Gather Information
Ask the user for:
1. Company domain (e.g., "acme.com") - used for Google OAuth
2. Data source type: keboola / csv / bigquery (future)
3. Instance name (e.g., "Acme Data Analyst")

### Step 2: Generate Configuration
1. Copy `config/instance.yaml.example` to `config/instance.yaml`
2. Fill in values from Step 1
3. If Keboola: ask for Storage API token, stack URL, project ID
4. Create `.env` from `config/.env.template`

### Step 3: Generate Data Description
1. If Keboola adapter: use the API to fetch table metadata and generate `docs/data_description.md`
2. If CSV: ask user to describe their data files
3. The file defines tables, sync strategies, and schema

### Step 4: Server Setup (if deploying)
1. Guide VM provisioning (or use existing server)
2. Run `server/setup.sh` on the target VM
3. Run `server/webapp-setup.sh` for the web portal
4. Set up CI/CD from `.github/workflows/deploy.yml.example`

## Project Structure

```
├── src/                    # Core data sync engine (vendor-neutral)
│   ├── config.py           # Configuration from data_description.md
│   ├── data_sync.py        # Sync orchestration + DataSource ABC
│   ├── parquet_manager.py  # Parquet file management
│   └── profiler.py         # Data profiling
├── connectors/             # Data source connectors (pluggable)
│   ├── keboola/            # Keboola Storage connector
│   └── jira/               # Jira webhook connector
├── auth/                   # Authentication providers (pluggable)
│   ├── google/             # Google OAuth provider
│   ├── email/              # Email magic link provider
│   └── desktop/            # Desktop JWT provider (API-only)
├── services/               # Standalone services (own systemd units)
│   ├── telegram_bot/       # Telegram notification bot
│   ├── ws_gateway/         # WebSocket notification gateway
│   ├── corporate_memory/   # AI knowledge aggregation
│   └── session_collector/  # Claude Code session collector
├── webapp/                 # Flask web portal (login, dashboard, API)
├── server/                 # Deployment infrastructure only
├── scripts/                # Utility scripts (sync, DuckDB setup, dev)
├── config/                 # Configuration templates
│   ├── instance.yaml.example
│   └── data_description.md.example
├── docs/                   # Documentation
│   └── metrics/            # Business metric YAML definitions
│       ├── revenue/        # Revenue metrics (total_revenue, AOV, etc.)
│       ├── customers/      # Customer metrics (count, repeat rate)
│       ├── marketing/      # Marketing metrics (ROI, CPA, conversion)
│       └── support/        # Support metrics (resolution time, CSAT)
└── tests/                  # Test suite
```

## Architecture

```
Data Source (Keboola / CSV / BigQuery)
      │
      ▼
┌─────────────────────────────────┐
│  Data Broker Server             │
│  ├── /data/src_data/parquet/    │  Converted data
│  ├── /data/docs/                │  Documentation
│  └── /data/scripts/             │  Helper scripts
└─────────────────────────────────┘
      │ rsync (via ~/server/ symlinks)
      ▼
┌─────────────────────────────────┐
│  Analyst (local machine)        │
│  ├── ./server/  (read-only)     │  parquet, docs, scripts
│  └── ./user/    (workspace)     │  duckdb, notifications
└─────────────────────────────────┘
```

## Configuration

Instance-specific config is in `config/instance.yaml`. See `config/instance.yaml.example` for all options.

Environment variables go in `.env` (never committed to git).

Data schema is defined in `docs/data_description.md` (YAML blocks in markdown).

### Dual-Repo Deployment
Production uses two repos on the server:
- **OSS repo** (`/opt/data-analyst/repo/`): application code, no secrets or config
- **Instance repo** (`/opt/data-analyst/instance/`): private config, secrets template, data schema

Symlinks bridge them: `repo/config/instance.yaml -> instance/config/instance.yaml`.
Each repo has its own SSH deploy key (github-oss / github-cfg aliases).
See `docs/auto-install.md` for full setup guide.

## Development

```bash
# Setup
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Run webapp locally
flask --app webapp.app run --debug

# Run tests
pytest tests/ -v

# Sync data
python -m src.data_sync
```

## Extensibility

### Data Sources
Pluggable data source connectors in `connectors/`:
- **Keboola** (`keboola`): Syncs from Keboola Storage API
- **CSV** (`csv`): Import from local CSV files (planned)
- New connector = `connectors/<name>/adapter.py` implementing `DataSource`

### Authentication
Pluggable auth providers in `auth/`:
- **Google** (`google`): OAuth via Google
- **Email** (`email`): Email magic link (itsdangerous token, no password needed)
- **Password** (`password`): Username/password authentication
- **Desktop** (`desktop`): JWT for desktop app API
- New provider = `auth/<name>/provider.py` implementing `AuthProvider`

Configure data source in `config/instance.yaml` under `data_source.type`.

## Server Management

```bash
# Add analyst user
sudo add-analyst username "ssh-rsa AAAA..."

# Add privileged analyst
sudo add-analyst username "ssh-rsa AAAA..." --private

# List analysts
list-analysts

# Server monitoring
uptime && free -h && df -h /data
```

## Returning Users

When reopening the project in Claude Code:
1. Sync latest data: `rsync -avz --no-perms --no-group data-analyst:server/parquet/ ./server/parquet/`
2. Verify DuckDB: `ls -lh user/duckdb/analytics.duckdb`
3. Start analyzing with Claude Code

## Key Implementation Details

### Config Loading Chain
1. `config/loader.py` loads `instance.yaml` (checks `$CONFIG_DIR`, then `./config/`)
2. `webapp/config.py` calls `_load_instance_config()` at module level
3. `_get(config, *keys, default="")` traverses nested dicts safely
4. `inject_config()` context processor exposes `Config` to all Jinja templates
5. Templates use `{{ config.INSTANCE_NAME }}`, `{{ config.INSTANCE_SUBTITLE }}`, etc.

### Connector Pattern
- ABC: `DataSource` class in `src/data_sync.py`
- Registry: `create_data_source()` in `src/data_sync.py` auto-discovers connectors in `connectors/`
- Keboola: `connectors/keboola/adapter.py` -> `KeboolaDataSource` implementing `DataSource`
- Core Keboola logic: `connectors/keboola/client.py` (Keboola Storage API wrapper)

### Auth Provider Pattern
- ABC: `AuthProvider` class in `auth/__init__.py`
- Discovery: `discover_providers()` scans `auth/*/provider.py`
- Providers: google, email, desktop (each exports `provider` instance)
- Email provider: uses `itsdangerous.URLSafeTimedSerializer` for magic link tokens
- Multi-domain: `auth.allowed_domain` in instance.yaml supports comma-separated domains
- Session contract: all providers set `session["user"] = {"email", "name", "picture"}`

### Service Pattern
- Self-contained modules in `services/` with `__main__.py` for `python -m services.<name>`
- Systemd files in `services/<name>/systemd/`, auto-discovered by `deploy.sh`
- Services: telegram_bot, ws_gateway, corporate_memory, session_collector

### Business Metrics Pattern
- YAML definitions in `docs/metrics/{category}/{metric}.yml` (list with one dict)
- `webapp/utils/metric_parser.py` - parses YAML, structures for modal UI, auto-discovers `sql_*` fields
- `webapp/app.py` `_load_metrics_data()` - scans metrics dir, groups by category, returns ordered list
- Catalog template renders dynamically via Jinja loop (no hardcoded metrics)
- Profiler links metrics to tables via `used_by_metrics` in `profiles.json`
- Production: metrics in instance repo deployed to `/data/docs/metrics/`
- Sample/dev: OSS repo `docs/metrics/` (10 e-commerce metrics)

### Table Registry Pattern
- `src/table_registry.py` - central CRUD for registered tables with atomic JSON persistence
- Audit logging for register/unregister operations
- Generates `data_description.md` from registry state

### Server Patterns
- Atomic JSON writes: `tempfile.mkstemp()` + `os.fchmod(fd, 0o660)` + `os.replace()`
- User home writes: `sudo /usr/bin/install -o {user} -g {user}` pattern
- Staging dir: `/tmp/data_analyst_staging` (deploy.sh creates it with setgid)
- Dev docs: `dev_docs/server.md` documents all established patterns

### Files NOT to modify (stable infrastructure)
- `src/parquet_manager.py` - Parquet conversion engine
- `connectors/jira/file_lock.py` - Advisory file locking
- `connectors/jira/incremental_transform.py` - Jira monthly Parquet transform
- `services/ws_gateway/` - WebSocket notification gateway

## Git Commits & Pull Requests

- Keep commit messages clean and concise
- Do not include AI attribution in commits or PRs
