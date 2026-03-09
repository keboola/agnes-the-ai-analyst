# Service Connector - Integration of Internal APIs into Data Analyst Platform

## Context

The data analyst platform currently supports only data analysis (parquet files + DuckDB). We want to extend it so analysts can also interact with internal services (Purchase Order system, Invoicing, CRM) through Claude Code. This requires:

1. **API keys** delivered to the analyst's local machine (`.env` file)
2. **Skills** teaching Claude Code how to use each service's API (`.claude/rules/` markdown files)
3. **Seamless UX** - non-technical users click "Connect" in the web portal, everything else is automatic

Key constraints:
- All external services are internal apps (we can modify them)
- They already have Google OAuth and Bearer token/API key authentication
- They already have token generation UI
- We target 2-3 services initially
- Must reuse established patterns (sudo install, atomic JSON, sync_data.sh)

## Architecture Overview

```
User clicks "Connect" on your-instance.example.com
    |
    v
Webapp calls external service's internal token-exchange endpoint
    |  (service-to-service, shared secret)
    v
API key returned, stored in /data/service-connectors/connections.json
    |
    v
Webapp writes /home/{user}/.service_env (sudo install, mode 600)
Webapp writes /home/{user}/.claude_rules/sc_{service}.md (skill file)
    |
    v
Analyst runs sync_data.sh
    |
    v
.service_env -> merged into ~/keboola-analysis/.env
sc_*.md -> already synced with existing corporate memory rules sync
```

## Implementation Plan

### Phase 1: Service Registry & Infrastructure

**1.1 Create service registry config**
- File: `docs/setup/service_connectors.json`
- Defines available services: id, name, description, URLs, env var names, skill file name
- Deployed to `/data/docs/setup/` by deploy.sh

**1.2 Create sudo helper script**
- File: `server/bin/install-service-env`
- Accepts: USERNAME, ENV_SOURCE_PATH, SKILLS_SOURCE_DIR
- Installs `.service_env` (mode 600) to user home
- Installs `sc_*.md` skill files to `.claude_rules/` (mode 600)
- Only removes `sc_*.md` files (leaves `km_*.md` from corporate memory intact)
- Template: `server/bin/install-user-rules` (63 lines, same structure)

**1.3 Update sudoers**
- File: `server/sudoers-webapp` - add entry for `install-service-env`

**1.4 Update deploy.sh**
- Create `/data/service-connectors/` directory (www-data:data-ops, 2770)
- Deploy service registry and skill files
- Add new env vars to .env block: `SC_SECRET_PURCHASE_ORDERS`, `SC_SECRET_INVOICING`, `SC_SECRET_CRM`

**1.5 Add config entries**
- File: `webapp/config.py` - no new config class entries needed (secrets read directly with `os.environ.get()` in the service module, same pattern as sync_settings_service.py)

### Phase 2: Backend Service

**2.1 Create service connector module**
- File: `webapp/service_connector_service.py`
- Pattern: follows `webapp/sync_settings_service.py` exactly

Key functions:
```python
# Data storage
CONNECTORS_DIR = Path(os.environ.get("CONNECTORS_DIR", "/data/service-connectors"))
CONNECTIONS_FILE = CONNECTORS_DIR / "connections.json"

# Core functions
def get_available_services() -> dict                          # Load registry
def get_user_connections(username: str) -> dict                # User's connection status
def connect_service(username, service_id, user_email) -> (bool, str)  # Token exchange + install
def disconnect_service(username, service_id) -> (bool, str)    # Revoke + cleanup
def check_service_health(service_id) -> dict                   # Health check

# Internal
def _exchange_token(service, user_email) -> dict | None        # Call external service
def _revoke_token(service, token_id) -> bool                   # Call revoke endpoint
def _regenerate_user_env(username) -> bool                     # Write .service_env via sudo
def _install_service_skills(username) -> bool                  # Write sc_*.md via sudo
def _get_server_username(webapp_username) -> str               # Reuse WEBAPP_TO_SERVER_USERNAME
```

Storage format (`connections.json`):
```json
{
  "john": {
    "purchase_orders": {
      "connected": true,
      "api_key": "pk_live_abc123...",
      "token_id": "tok_xyz789",
      "connected_at": "2026-02-16T12:00:00Z",
      "expires_at": "2026-05-17T12:00:00Z"
    }
  }
}
```

Note: API keys stored in connections.json (protected by 660 permissions, www-data:data-ops). This follows the same approach as telegram_users.json storing chat_ids. For internal services, this is acceptable security level.

**2.2 Add API routes to webapp**
- File: `webapp/app.py` - add routes in `register_routes()`

```
GET  /api/service-connectors          - List services + user connections
POST /api/service-connectors/connect  - Connect to a service {service_id}
POST /api/service-connectors/disconnect - Disconnect {service_id}
GET  /api/service-connectors/health/<service_id> - Health check
```

**2.3 Token exchange protocol**
What each external service needs to implement:

```
POST /api/internal/token-exchange
Authorization: Bearer <shared_secret>
Body: {"user_email": "john@your-domain.com", "ttl_days": 90}
Response: {"status": "ok", "api_key": "...", "token_id": "...", "expires_at": "..."}

POST /api/internal/token-revoke
Authorization: Bearer <shared_secret>
Body: {"token_id": "tok_xyz789"}
Response: {"status": "ok"}
```

### Phase 3: Dashboard UI

**3.1 Add Service Connectors card to dashboard**
- File: `webapp/templates/dashboard.html`
- New card in the existing 2-column layout (same pattern as Data Settings and Telegram cards)
- Shows grid of service cards with Connect/Disconnect buttons
- Connected = green badge + expiry date
- AJAX calls to `/api/service-connectors/*` endpoints

### Phase 4: Sync & Skills

**4.1 Extend sync_data.sh**
- File: `scripts/sync_data.sh`
- Add block after corporate memory rules sync (line ~418):
  1. Download `~/.service_env` from server via SCP
  2. If exists: merge into local `.env` using marker comments (`# --- SERVICE CONNECTOR START/END ---`)
  3. If not exists: clean old service connector block from `.env`

```bash
# --- Sync service connector credentials ---
if scp -q data-analyst:~/.service_env /tmp/.service_env_$$ 2>/dev/null; then
    # Remove old block, append new one with markers
    sed -i.bak '/^# --- SERVICE CONNECTOR START ---$/,/^# --- SERVICE CONNECTOR END ---$/d' ./.env 2>/dev/null
    { echo "# --- SERVICE CONNECTOR START ---"; cat /tmp/.service_env_$$; echo "# --- SERVICE CONNECTOR END ---"; } >> ./.env
    rm -f /tmp/.service_env_$$
fi
```

Note: `sc_*.md` skills are already synced by the existing corporate memory sync block (line 410: `scp -rq "data-analyst:~/.claude_rules/"* .claude/rules/`).

**4.2 Create skill files**
- Directory: `docs/service_connector_skills/`
- Files: `sc_purchase_orders.md`, `sc_invoicing.md`, `sc_crm.md`
- Content: Authentication setup, available endpoints, common patterns, data models
- Deployed to `/data/docs/service_connector_skills/` by deploy.sh
- Installed to user's `.claude_rules/` when they connect

### Phase 5: Tests

**5.1 Unit tests**
- File: `tests/test_service_connector_service.py`
- Test: connect/disconnect flow, env generation, registry loading, error handling

## Files to Create

| File | Purpose |
|------|---------|
| `webapp/service_connector_service.py` | Core service (connect, disconnect, env generation) |
| `docs/setup/service_connectors.json` | Service registry config |
| `docs/service_connector_skills/sc_purchase_orders.md` | PO API skill |
| `server/bin/install-service-env` | Sudo helper for env + skills install |
| `tests/test_service_connector_service.py` | Unit tests |

## Files to Modify

| File | Change |
|------|--------|
| `webapp/app.py` | Import service_connector_service, add 4 API routes |
| `webapp/templates/dashboard.html` | Add Service Connectors card widget |
| `server/sudoers-webapp` | Add `install-service-env` entry |
| `server/deploy.sh` | Create /data/service-connectors/, deploy skills, add env vars |
| `scripts/sync_data.sh` | Add .service_env download and .env merge block |
| `.github/workflows/deploy.yml` | Add SC_SECRET_* GitHub Secrets to env |

## Key Patterns Reused

- **Sudo install**: `sync_settings_service.py:_regenerate_user_config()` (line 143-183)
- **Atomic JSON**: `sync_settings_service.py:_write_json()` (line 61-74)
- **Username mapping**: `corporate_memory_service.py:_get_server_username()` (line 56-59)
- **Sudo helper script**: `server/bin/install-user-rules` (entire file)
- **Dashboard AJAX pattern**: Sync settings toggles in `dashboard.html`

## Security Model

| Stage | Protection |
|-------|------------|
| Token exchange (webapp <-> service) | HTTPS + shared secret in Authorization header |
| Central storage (connections.json) | /data/service-connectors/ (2770), file 660 |
| User home (.service_env) | Mode 600 (owner-only), sudo install |
| Transit (sync) | SCP over SSH |
| Client (.env) | Local filesystem; Claude Code settings deny Read(.env) |
| Claude Code usage | Python `load_dotenv()` via Bash (allowed) |

## Verification

1. **Unit tests**: `pytest tests/test_service_connector_service.py`
2. **Manual flow**:
   - Deploy to server
   - Log into your-instance.example.com
   - Click "Connect" on PO system in dashboard
   - Verify `.service_env` appears in `/home/{user}/`
   - Run `sync_data.sh` on client
   - Verify `.env` contains PO_API_KEY
   - Verify `.claude/rules/sc_purchase_orders.md` exists
   - In Claude Code: `python -c "from dotenv import load_dotenv; load_dotenv(); import os; print(os.environ.get('PO_API_KEY', 'NOT SET'))"`
3. **Disconnect flow**: Click Disconnect, verify key removed from .env after sync
