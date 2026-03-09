# Architecture

## System Overview

```
Data Source (Keboola / CSV / BigQuery)
      |
      v
+------------------------------------------+
|  Data Broker Server                      |
|                                          |
|  src/data_sync.py                        |
|    -> connectors/*.py (fetch data)       |
|    -> src/parquet_manager.py (convert)   |
|                                          |
|  /data/src_data/parquet/   (output)      |
|  /data/docs/               (synced docs) |
|  /data/scripts/            (helpers)     |
+------------------------------------------+
      | rsync over SSH
      v
+------------------------------------------+
|  Analyst Machine                         |
|                                          |
|  server/parquet/  -> DuckDB views        |
|  user/duckdb/analytics.duckdb            |
|  Claude Code queries DuckDB via SQL      |
+------------------------------------------+
```

## Components

### 1. Data Sync Engine (`src/`)

Pulls data from configured source, converts to Parquet.

| File | Role |
|------|------|
| `src/data_sync.py` | Orchestration + `DataSource` ABC (line 149) |
| `connectors/keboola/adapter.py` | Keboola data source |
| `connectors/keboola/client.py` | Low-level Keboola API client |
| `src/parquet_manager.py` | CSV -> typed Parquet conversion |
| `src/config.py` | Reads `data_description.md` for table definitions |
| `src/profiler.py` | Data profiling for catalog UI |

### 2. Web Application (`webapp/`)

Flask app for user onboarding, settings, and data catalog.

| File | Role |
|------|------|
| `webapp/app.py` | Flask entry point, routes |
| `webapp/config.py` | Loads `instance.yaml`, exposes `Config` to templates |
| `webapp/account_service.py` | User account details, sync status |
| `webapp/templates/` | Jinja2 templates (dashboard, setup, catalog) |

### 3. Configuration (`config/`)

| File | Role |
|------|------|
| `config/instance.yaml` | Main instance config (not committed) |
| `config/instance.yaml.example` | Template with all options |
| `config/loader.py` | YAML loader with `${ENV_VAR}` interpolation |
| `config/.env.template` | Secret variable placeholders |
| `docs/data_description.md` | Table schemas + sync strategies (not committed) |

### 4. Auth Providers (`auth/`)

Pluggable authentication via auto-discovered providers.

| File | Role |
|------|------|
| `auth/__init__.py` | `AuthProvider` ABC + `discover_providers()` scanner |
| `auth/google/provider.py` | Google OAuth (extracted from webapp/auth.py) |
| `auth/password/provider.py` | Email/password (delegates to webapp/password_auth) |
| `auth/desktop/provider.py` | Desktop JWT auth (API-only, hidden from login page) |

To add a new provider: create `auth/<name>/provider.py` implementing `AuthProvider`, export a `provider` instance. No core changes needed.

### 5. Standalone Services (`services/`)

Self-contained services with own systemd units, auto-discovered by `deploy.sh`.

| Directory | Role |
|-----------|------|
| `services/telegram_bot/` | Telegram notification bot + dispatch |
| `services/ws_gateway/` | WebSocket gateway for desktop app |
| `services/corporate_memory/` | AI knowledge aggregation from analyst sessions |
| `services/session_collector/` | Claude Code session metadata collector |

### 6. Server Infrastructure (`server/`)

Deployment only -- no application code.

| File | Role |
|------|------|
| `server/setup.sh` | Initial server provisioning (groups, users, dirs) |
| `server/webapp-setup.sh` | Nginx, SSL, systemd for webapp |
| `server/deploy.sh` | CI/CD deployment (auto-discovers `services/*/systemd/*`) |
| `server/sudoers-deploy` | Least-privilege sudo rules for deploy user |
| `server/sudoers-webapp` | Sudo rules for www-data (webapp) |
| `server/bin/` | Management scripts (add-analyst, list-analysts, etc.) |

### 7. Analyst Scripts (`scripts/`)

Helper scripts synced to analyst machines.

| File | Role |
|------|------|
| `scripts/sync_data.sh` | Sync data from server via rsync |
| `scripts/setup_views.sh` | Create DuckDB views over Parquet files |

## Config Loading Chain

```
config/instance.yaml
    |  (loaded by config/loader.py)
    |  (${ENV_VAR} references resolved from .env / environment)
    v
webapp/config.py
    |  (_load_instance_config at module level)
    |  (_get(config, *keys) for safe nested access)
    v
inject_config() context processor
    |  (exposes Config object to all Jinja templates)
    v
{{ config.INSTANCE_NAME }} in templates
```

## Data Flow

```
1. Admin defines tables in docs/data_description.md
2. src/config.py parses YAML blocks from markdown
3. src/data_sync.py iterates tables, calls adapter
4. Adapter fetches CSV/JSON from source API
5. src/parquet_manager.py converts to typed Parquet
6. Parquet files stored in /data/src_data/parquet/
7. Analyst runs scripts/sync_data.sh (rsync over SSH)
8. scripts/setup_views.sh creates DuckDB views
9. Claude Code queries DuckDB, returns insights
```

## Security Model

- **Groups**: `data-ops` (admins), `dataread` (analysts), `data-private` (privileged)
- **Sudoers**: Explicit command whitelisting (no wildcards)
- **SSH**: Key-based auth only, keys registered via webapp
- **OAuth**: Google domain restriction via `auth.allowed_domain`
- **Secrets**: `${ENV_VAR}` in YAML, actual values in `.env` (gitignored)
- **Staging**: `/tmp/data_analyst_staging` with setgid for group ownership

## Key Patterns

- **Connector pattern**: Dynamic connector registry in `src/data_sync.py`, `connectors/keboola/` for reference
- **Auth provider pattern**: Auto-discovered from `auth/*/provider.py`, each implements `AuthProvider` ABC
- **Service pattern**: Self-contained modules in `services/` with own `__main__.py` and `systemd/` directory
- **Atomic writes**: `tempfile.mkstemp()` + `os.fchmod()` + `os.replace()` for JSON state files
- **User home writes**: `sudo install -o {user} -g {user}` for writing to analyst home dirs
- **Config interpolation**: `${ENV_VAR}` in YAML resolved at load time, missing vars logged as warnings
