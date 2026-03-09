# Service Connector - Integration of Internal APIs into Data Analyst Platform

## Origin

This plan was derived from two independent proposals (Claude and Codex), both reviewed by 3 AI models (6 reviews total). The reviews identified real issues but also pushed the design toward over-engineering. This final version applies KISS and YAGNI to keep only what matters.

**Previous drafts** (kept for reference):
- `services-integration-claude.md` - Claude plan (good patterns, missing encryption)
- `services-integrations-codex.md` - Codex plan (SSH runtime injection, encrypted store - overkill for pilot)

**What we cut and why:**
- Fernet encryption of connections.json - encryption key lives on the same server as the data, security theater
- fcntl file locking - 5-10 users clicking Connect once a month, race condition probability ~0%
- Transactional connect with rollback - if token exchange fails, user clicks again
- Audit log + HMAC + logrotate - Flask access log is enough for 5 users
- Auto-rotation timer + systemd units - set long TTL, user reconnects if expired
- Feature flag rollout - just deploy when ready
- Security tests (CSRF, injection) - internal tool behind Google OAuth, all users are employees

## Context

The data analyst platform supports data analysis (parquet + DuckDB). We want analysts to also interact with internal services (Purchase Orders, Invoicing, CRM) through Claude Code.

**What the analyst needs:**
1. API key in their local `.env` file
2. Skill file in `.claude/rules/` teaching Claude Code how to use the API

**What we already have:**
- `sync_data.sh` that syncs files from server to analyst's machine
- `.claude_rules/` sync for corporate memory (skills already flow through this)
- `sudo install` pattern for writing to user home dirs
- Dashboard with AJAX cards (Data Settings, Telegram)

**Trust model:** Employee laptops are trusted (corporate-managed, encrypted disks). If a laptop is compromised, we have bigger problems than API keys.

## Architecture Overview

```
User clicks "Connect" on your-instance.example.com
    |
    v
Webapp calls service's token-exchange endpoint (shared secret)
    |
    v
API key stored in /data/service-connectors/connections.json (plaintext, 660)
    |
    v
Webapp writes /home/{user}/.service_env (sudo install, mode 600)
Webapp writes /home/{user}/.claude_rules/sc_{service}.md (skill file)
    |
    v
Analyst runs sync_data.sh (existing)
    |
    v
.service_env -> merged into local .env
sc_*.md -> already synced via existing .claude_rules/ sync
```

That's it. No encryption layer, no file locking, no audit log, no rotation timer.

## Implementation

### 1. Service Registry

File: `docs/setup/service_connectors.json`

```json
[
  {
    "id": "purchase_orders",
    "name": "Purchase Order System",
    "description": "Create and query purchase orders",
    "token_exchange_url": "https://po.internal.example.com/api/internal/token-exchange",
    "token_revoke_url": "https://po.internal.example.com/api/internal/token-revoke",
    "env_var_name": "PO_API_KEY",
    "skill_file": "sc_purchase_orders.md",
    "enabled": true
  }
]
```

Deployed to `/data/docs/setup/` by deploy.sh (same as other config files).

### 2. Backend Service

File: `webapp/service_connector_service.py`

Follows `webapp/sync_settings_service.py` pattern exactly.

```python
"""
Service connector - manages API key provisioning for internal services.

Reads service registry from /data/docs/setup/service_connectors.json.
Stores user connections in /data/service-connectors/connections.json.
Writes .service_env and skill files to user home dirs via sudo install.
"""

import json
import logging
import os
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

CONNECTORS_DIR = Path(os.environ.get("CONNECTORS_DIR", "/data/service-connectors"))
CONNECTIONS_FILE = CONNECTORS_DIR / "connections.json"
REGISTRY_FILE = Path(os.environ.get("REGISTRY_FILE", "/data/docs/setup/service_connectors.json"))
SKILLS_DIR = Path(os.environ.get("SC_SKILLS_DIR", "/data/docs/service_connector_skills"))

# Username mapping (reuse existing pattern)
WEBAPP_TO_SERVER_USERNAME = {
    # Add overrides here if webapp username != server username
    # "jane.smith": "jane",
}


def get_available_services() -> list[dict]:
    """Load service registry."""
    if not REGISTRY_FILE.exists():
        return []
    with open(REGISTRY_FILE) as f:
        services = json.load(f)
    return [s for s in services if s.get("enabled", True)]


def get_user_connections(username: str) -> dict:
    """Get user's active connections (without API keys)."""
    connections = _load_connections()
    user_conns = connections.get(username, {})
    # Strip API keys from response
    safe = {}
    for service_id, conn in user_conns.items():
        safe[service_id] = {
            "connected": conn.get("connected", False),
            "connected_at": conn.get("connected_at"),
            "expires_at": conn.get("expires_at"),
        }
    return safe


def connect_service(username: str, service_id: str, user_email: str) -> tuple[bool, str]:
    """Exchange token with service, store key, install to user home."""
    service = _get_service_config(service_id)
    if not service:
        return False, "Unknown service"

    # Get shared secret for this service
    secret_env = f"SC_SECRET_{service_id.upper()}"
    shared_secret = os.environ.get(secret_env)
    if not shared_secret:
        logger.error(f"Missing {secret_env} environment variable")
        return False, "Service not configured"

    # Token exchange
    try:
        resp = httpx.post(
            service["token_exchange_url"],
            headers={"Authorization": f"Bearer {shared_secret}"},
            json={"user_email": user_email, "ttl_days": 365},
            timeout=30,
        )
        resp.raise_for_status()
        token_data = resp.json()
    except Exception as e:
        logger.error(f"Token exchange failed for {service_id}: {e}")
        return False, f"Token exchange failed: {e}"

    # Store in connections.json
    connections = _load_connections()
    connections.setdefault(username, {})[service_id] = {
        "connected": True,
        "api_key": token_data["api_key"],
        "token_id": token_data.get("token_id"),
        "connected_at": datetime.utcnow().isoformat() + "Z",
        "expires_at": token_data.get("expires_at"),
    }
    _save_connections(connections)

    # Write .service_env and skills to user home
    server_username = _get_server_username(username)
    _regenerate_user_env(server_username, connections.get(username, {}))
    _install_service_skills(server_username, connections.get(username, {}))

    return True, "Connected successfully"


def disconnect_service(username: str, service_id: str) -> tuple[bool, str]:
    """Revoke token and remove credentials."""
    connections = _load_connections()
    conn = connections.get(username, {}).get(service_id)
    if not conn:
        return False, "Not connected"

    # Try to revoke remotely (best-effort)
    service = _get_service_config(service_id)
    token_id = conn.get("token_id")
    if service and token_id:
        try:
            secret_env = f"SC_SECRET_{service_id.upper()}"
            shared_secret = os.environ.get(secret_env, "")
            httpx.post(
                service["token_revoke_url"],
                headers={"Authorization": f"Bearer {shared_secret}"},
                json={"token_id": token_id},
                timeout=30,
            )
        except Exception as e:
            logger.warning(f"Remote revoke failed for {service_id}/{token_id}: {e}")

    # Always clean up locally
    connections.get(username, {}).pop(service_id, None)
    if username in connections and not connections[username]:
        del connections[username]
    _save_connections(connections)

    # Regenerate user files
    server_username = _get_server_username(username)
    _regenerate_user_env(server_username, connections.get(username, {}))
    _install_service_skills(server_username, connections.get(username, {}))

    return True, "Disconnected"


# --- Internal helpers ---

def _get_service_config(service_id: str) -> dict | None:
    """Find service in registry by ID."""
    for s in get_available_services():
        if s["id"] == service_id:
            return s
    return None


def _get_server_username(webapp_username: str) -> str:
    """Map webapp username to server Linux username."""
    return WEBAPP_TO_SERVER_USERNAME.get(webapp_username, webapp_username)


def _load_connections() -> dict:
    """Load connections.json."""
    if not CONNECTIONS_FILE.exists():
        return {}
    with open(CONNECTIONS_FILE) as f:
        return json.load(f)


def _save_connections(data: dict) -> None:
    """Atomic write to connections.json (same pattern as sync_settings_service)."""
    fd, temp_path = tempfile.mkstemp(dir=str(CONNECTORS_DIR), suffix=".json")
    try:
        os.write(fd, json.dumps(data, indent=2).encode())
        os.close(fd)
        os.chmod(temp_path, 0o660)
        os.replace(temp_path, str(CONNECTIONS_FILE))
    except Exception:
        os.close(fd) if not os.get_inheritable(fd) else None
        os.unlink(temp_path)
        raise


def _regenerate_user_env(server_username: str, user_connections: dict) -> None:
    """Write .service_env to user home via sudo install."""
    # Build env file content
    lines = []
    for service_id, conn in user_connections.items():
        if not conn.get("connected"):
            continue
        service = _get_service_config(service_id)
        if service:
            lines.append(f"{service['env_var_name']}={conn['api_key']}")

    # Write to temp file, then sudo install
    fd, temp_path = tempfile.mkstemp(suffix=".env")
    try:
        os.write(fd, "\n".join(lines).encode() if lines else b"")
        os.close(fd)

        target = f"/home/{server_username}/.service_env"
        if lines:
            subprocess.run(
                ["sudo", "/usr/bin/install", "-o", server_username, "-g", server_username,
                 "-m", "600", temp_path, target],
                check=True, capture_output=True,
            )
        else:
            # No connections - remove .service_env if it exists
            subprocess.run(
                ["sudo", "rm", "-f", target],
                check=True, capture_output=True,
            )
    finally:
        os.unlink(temp_path)


def _install_service_skills(server_username: str, user_connections: dict) -> None:
    """Install sc_*.md skill files to user's .claude_rules/ via sudo helper."""
    connected_services = [
        sid for sid, conn in user_connections.items() if conn.get("connected")
    ]

    # Copy relevant skill files to temp dir
    temp_dir = tempfile.mkdtemp()
    try:
        for service_id in connected_services:
            service = _get_service_config(service_id)
            if service and service.get("skill_file"):
                src = SKILLS_DIR / service["skill_file"]
                if src.exists():
                    dest = Path(temp_dir) / service["skill_file"]
                    dest.write_bytes(src.read_bytes())

        subprocess.run(
            ["sudo", "/usr/local/bin/install-service-env",
             server_username, temp_dir],
            check=True, capture_output=True,
        )
    finally:
        import shutil
        shutil.rmtree(temp_dir, ignore_errors=True)
```

### 3. API Routes

Add to `webapp/app.py` in `register_routes()`:

```python
from webapp import service_connector_service

@app.route("/api/service-connectors")
@login_required
def api_service_connectors():
    username = get_username_from_email(session["user"]["email"])
    services = service_connector_service.get_available_services()
    connections = service_connector_service.get_user_connections(username)
    return jsonify({"services": services, "connections": connections})

@app.route("/api/service-connectors/connect", methods=["POST"])
@login_required
def api_service_connect():
    username = get_username_from_email(session["user"]["email"])
    email = session["user"]["email"]
    service_id = request.json.get("service_id")
    ok, msg = service_connector_service.connect_service(username, service_id, email)
    return jsonify({"success": ok, "message": msg})

@app.route("/api/service-connectors/disconnect", methods=["POST"])
@login_required
def api_service_disconnect():
    username = get_username_from_email(session["user"]["email"])
    service_id = request.json.get("service_id")
    ok, msg = service_connector_service.disconnect_service(username, service_id)
    return jsonify({"success": ok, "message": msg})
```

### 4. Sudo Helper

File: `server/bin/install-service-env` (copy of `install-user-rules`, modified for `sc_*` prefix)

```bash
#!/bin/bash
# Install service connector skill files to user's .claude_rules/.
# Called by webapp (www-data) via sudo.
#
# Usage: sudo install-service-env USERNAME SKILLS_SOURCE_DIR

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Must be run as root (via sudo)" >&2
    exit 1
fi

if [[ $# -lt 2 ]]; then
    echo "Usage: sudo install-service-env USERNAME SKILLS_SOURCE_DIR" >&2
    exit 1
fi

USERNAME="$1"
SOURCE_DIR="$2"

if ! id "$USERNAME" &>/dev/null; then
    echo "User '$USERNAME' does not exist" >&2
    exit 1
fi

if [[ ! -d "$SOURCE_DIR" ]]; then
    echo "Source directory '$SOURCE_DIR' does not exist" >&2
    exit 1
fi

USER_HOME=$(eval echo "~${USERNAME}")
RULES_DIR="${USER_HOME}/.claude_rules"

mkdir -p "$RULES_DIR"
chown "${USERNAME}:${USERNAME}" "$RULES_DIR"
chmod 700 "$RULES_DIR"

# Remove old service connector skills only (sc_*.md), preserve km_*.md
rm -f "${RULES_DIR}"/sc_*.md

# Install new skill files
COUNT=0
for src_file in "${SOURCE_DIR}"/*.md; do
    if [[ -f "$src_file" ]]; then
        /usr/bin/install -o "$USERNAME" -g "$USERNAME" -m 600 "$src_file" "$RULES_DIR/"
        COUNT=$((COUNT + 1))
    fi
done

echo "Installed ${COUNT} service skills for ${USERNAME} in ${RULES_DIR}"
```

### 5. Dashboard UI Card

Add to `webapp/templates/dashboard.html` - new card in existing grid, same AJAX pattern as Data Settings toggles:

- Grid of service cards (name + description from registry)
- Green "Connected" badge or grey "Not connected"
- Connect / Disconnect button
- Shows `expires_at` if connected

### 6. Sync Extension

Add to `scripts/sync_data.sh` after existing corporate memory sync block:

```bash
# --- Sync service connector credentials ---
if scp -q data-analyst:~/.service_env /tmp/.service_env_$$ 2>/dev/null; then
    # Remove old service connector block
    if [ -f ./.env ]; then
        awk '
            /^# --- SERVICE CONNECTOR START ---$/ { skip=1; next }
            /^# --- SERVICE CONNECTOR END ---$/ { skip=0; next }
            !skip { print }
        ' ./.env > ./.env.tmp && mv ./.env.tmp ./.env
    fi
    # Append new block
    {
        echo "# --- SERVICE CONNECTOR START ---"
        cat /tmp/.service_env_$$
        echo "# --- SERVICE CONNECTOR END ---"
    } >> ./.env
    rm -f /tmp/.service_env_$$
    echo "Service connector credentials synced"
else
    # No active connections - clean old block if present
    if [ -f ./.env ] && grep -q "^# --- SERVICE CONNECTOR START ---$" ./.env 2>/dev/null; then
        awk '
            /^# --- SERVICE CONNECTOR START ---$/ { skip=1; next }
            /^# --- SERVICE CONNECTOR END ---$/ { skip=0; next }
            !skip { print }
        ' ./.env > ./.env.tmp && mv ./.env.tmp ./.env
        echo "Service connector credentials removed"
    fi
fi
```

Skill files (`sc_*.md`) are already synced by the existing `.claude_rules/` sync block.

### 7. Deploy Changes

Add to `server/deploy.sh`:

```bash
# Service connectors directory
mkdir -p /data/service-connectors
chown www-data:data-ops /data/service-connectors
chmod 2770 /data/service-connectors

# Deploy skill files
mkdir -p /data/docs/service_connector_skills
cp -r docs/service_connector_skills/* /data/docs/service_connector_skills/ 2>/dev/null || true

# Deploy service registry
cp docs/setup/service_connectors.json /data/docs/setup/ 2>/dev/null || true

# Install sudo helper
install -m 755 server/bin/install-service-env /usr/local/bin/
```

Add to `server/sudoers-webapp`:
```
www-data ALL=(ALL) NOPASSWD: /usr/local/bin/install-service-env
```

Add to `.github/workflows/deploy.yml` env block:
```yaml
SC_SECRET_PURCHASE_ORDERS: ${{ secrets.SC_SECRET_PURCHASE_ORDERS }}
```

## Token Exchange Protocol

What each internal service needs to implement (simple Bearer + JSON):

```
POST /api/internal/token-exchange
Authorization: Bearer <shared_secret>
Content-Type: application/json
Body: {"user_email": "john@your-domain.com", "ttl_days": 365}
Response: {"status": "ok", "api_key": "...", "token_id": "...", "expires_at": "..."}

POST /api/internal/token-revoke
Authorization: Bearer <shared_secret>
Content-Type: application/json
Body: {"token_id": "tok_xyz789"}
Response: {"status": "ok"}
```

TTL is 365 days. If a key expires, user clicks Reconnect. No auto-rotation needed.

## Files to Create

| File | Purpose | Size estimate |
|------|---------|---------------|
| `webapp/service_connector_service.py` | Connect, disconnect, env generation | ~150 lines |
| `docs/setup/service_connectors.json` | Service registry | ~20 lines |
| `docs/service_connector_skills/sc_purchase_orders.md` | PO API skill for Claude Code | ~50 lines |
| `server/bin/install-service-env` | Sudo helper (copy of install-user-rules) | ~30 lines |
| `tests/test_service_connector_service.py` | Unit tests | ~100 lines |

## Files to Modify

| File | Change | Size estimate |
|------|--------|---------------|
| `webapp/app.py` | Add 3 API routes | ~20 lines |
| `webapp/templates/dashboard.html` | Service connectors card | ~60 lines |
| `server/sudoers-webapp` | Add install-service-env entry | 1 line |
| `server/deploy.sh` | Create dirs, deploy skills, add env vars | ~10 lines |
| `scripts/sync_data.sh` | .service_env merge block | ~20 lines |
| `.github/workflows/deploy.yml` | Add SC_SECRET_* to env | ~3 lines |

**Total new code: ~350 lines** (vs ~800+ in the hybrid plan, ~1200+ in the Codex plan)

## Security Model

| Stage | Protection |
|-------|------------|
| Token exchange | HTTPS + per-service shared secret |
| Server storage (connections.json) | File permissions 660, dir 2770 (www-data:data-ops) |
| User home (.service_env) | Mode 600, sudo install |
| Transit | SCP over SSH |
| Client (.env) | Local filesystem, Claude Code denies Read(.env) |
| Trust model | Employee laptops trusted (corporate-managed, encrypted disks) |

## Key Patterns Reused

- **Sudo install**: `sync_settings_service.py:_regenerate_user_config()`
- **Atomic JSON write**: `sync_settings_service.py:_write_json()` (tempfile + os.replace)
- **Username mapping**: `corporate_memory_service.py:_get_server_username()`
- **Sudo helper**: `server/bin/install-user-rules` (same structure)
- **Dashboard AJAX**: Sync settings toggles in `dashboard.html`

## Verification

1. `pytest tests/test_service_connector_service.py`
2. Deploy, click Connect on PO, verify `.service_env` in `/home/{user}/`
3. Run `sync_data.sh`, verify `.env` contains `PO_API_KEY`
4. Verify `.claude/rules/sc_purchase_orders.md` exists
5. In Claude Code: `python -c "from dotenv import load_dotenv; load_dotenv(); import os; print(os.environ.get('PO_API_KEY', 'NOT SET'))"`
6. Click Disconnect, sync, verify key removed

## What We Might Add Later (only if needed)

| Feature | When to add |
|---------|-------------|
| Encryption of connections.json | If we get a compliance requirement |
| Auto-rotation | If services start issuing short-lived tokens |
| Audit log | If we need forensics capability |
| File locking | If we ever have concurrent connect/disconnect issues |
| SSH runtime injection | If laptop trust model changes |
