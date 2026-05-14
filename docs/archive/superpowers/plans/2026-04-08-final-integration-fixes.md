# Final Integration Fixes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix all remaining integration gaps after the v2 refactoring so the system is fully operational — Jira webhooks, Docker services, dynamic login, profiler auto-trigger, scheduler auth.

**Architecture:** Five independent fixes targeting: (1) Jira webhook FastAPI adapter, (2) Docker compose service entries, (3) dynamic auth provider detection on login page, (4) profiler integration with sync pipeline, (5) scheduler auto-authentication.

**Tech Stack:** Python 3.11+, FastAPI, DuckDB, Docker Compose, httpx

---

### Task 1: Jira Webhook FastAPI Adapter

**Files:**
- Create: `app/api/jira_webhooks.py`
- Modify: `app/main.py`
- Modify: `connectors/jira/service.py` (add `JIRA_WEBHOOK_SECRET` to `_JiraConfig`)
- Test: `tests/test_jira_webhooks.py`

- [ ] **Step 1: Add JIRA_WEBHOOK_SECRET to config**

```python
# connectors/jira/service.py — add to _JiraConfig class (line ~24)
JIRA_WEBHOOK_SECRET = os.environ.get("JIRA_WEBHOOK_SECRET", "")
DEBUG = os.environ.get("DEBUG", "").lower() in ("1", "true")
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_jira_webhooks.py
"""Tests for Jira webhook FastAPI adapter."""
import hashlib
import hmac
import json
import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def webhook_client(tmp_path):
    os.environ["DATA_DIR"] = str(tmp_path)
    os.environ["JWT_SECRET_KEY"] = "test-secret-32chars-minimum!!"
    os.environ["JIRA_WEBHOOK_SECRET"] = "test-webhook-secret"
    (tmp_path / "state").mkdir()
    (tmp_path / "extracts" / "jira" / "raw" / "webhook_events").mkdir(parents=True)

    from app.main import create_app
    app = create_app()
    return TestClient(app)


def _sign(payload: bytes, secret: str = "test-webhook-secret") -> str:
    return "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


class TestJiraWebhook:
    def test_health(self, webhook_client):
        resp = webhook_client.get("/webhooks/jira/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_missing_signature_401(self, webhook_client):
        resp = webhook_client.post("/webhooks/jira", json={"webhookEvent": "test"})
        assert resp.status_code == 401

    def test_invalid_signature_401(self, webhook_client):
        body = json.dumps({"webhookEvent": "test"}).encode()
        resp = webhook_client.post(
            "/webhooks/jira", content=body,
            headers={"X-Hub-Signature-256": "sha256=invalid", "Content-Type": "application/json"},
        )
        assert resp.status_code == 401

    def test_valid_signature_accepted(self, webhook_client):
        body = json.dumps({
            "webhookEvent": "jira:issue_updated",
            "issue": {"key": "TEST-1", "fields": {"summary": "Test"}},
        }).encode()
        sig = _sign(body)
        resp = webhook_client.post(
            "/webhooks/jira", content=body,
            headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"},
        )
        # 200 or 503 (Jira not configured) — but NOT 401
        assert resp.status_code in (200, 503)

    def test_empty_payload_400(self, webhook_client):
        body = b""
        sig = _sign(body)
        resp = webhook_client.post(
            "/webhooks/jira", content=body,
            headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"},
        )
        assert resp.status_code == 400
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_jira_webhooks.py -v`
Expected: FAIL — no `/webhooks/jira` route

- [ ] **Step 4: Create the FastAPI adapter**

```python
# app/api/jira_webhooks.py
"""Jira webhook endpoint — FastAPI adapter for connectors/jira/webhook.py logic."""

import hashlib
import hmac
import json
import logging
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from connectors.jira.service import Config, get_jira_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks", tags=["webhooks"])

WEBHOOK_LOG_DIR = Config.JIRA_DATA_DIR / "webhook_events"


def _verify_signature(payload: bytes, signature: str | None) -> bool:
    secret = Config.JIRA_WEBHOOK_SECRET
    if not secret:
        logger.warning("JIRA_WEBHOOK_SECRET not configured, skipping verification")
        return True
    if not signature:
        return False
    if signature.startswith("sha256="):
        signature = signature[7:]
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def _log_event(event_data: dict) -> None:
    try:
        WEBHOOK_LOG_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
        event_type = event_data.get("webhookEvent", "unknown").replace(":", "_")
        path = WEBHOOK_LOG_DIR / f"{ts}_{event_type}.json"
        path.write_text(json.dumps(event_data, indent=2, default=str))
    except Exception as e:
        logger.warning("Failed to log webhook event: %s", e)


@router.post("/jira")
async def receive_jira_webhook(request: Request):
    payload = await request.body()
    signature = request.headers.get("X-Hub-Signature-256") or request.headers.get("X-Hub-Signature")

    if not _verify_signature(payload, signature):
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        event_data = json.loads(payload) if payload else None
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid JSON")

    if not event_data:
        raise HTTPException(status_code=400, detail="Empty payload")

    _log_event(event_data)

    webhook_event = event_data.get("webhookEvent", "unknown")
    issue_key = event_data.get("issue", {}).get("key", "unknown")
    logger.info("Received webhook: %s for %s", webhook_event, issue_key)

    jira_service = get_jira_service()
    if not jira_service.is_configured():
        return {"status": "error", "message": "Jira not configured"}, 503

    success = jira_service.process_webhook_event(event_data)

    if success:
        return {"status": "ok", "event": webhook_event, "issue": issue_key}
    raise HTTPException(status_code=500, detail="Failed to process event")


@router.get("/jira/health")
async def jira_webhook_health():
    jira_service = get_jira_service()
    return {
        "status": "ok",
        "configured": jira_service.is_configured(),
        "webhook_secret_set": bool(Config.JIRA_WEBHOOK_SECRET),
        "jira_domain": Config.JIRA_DOMAIN or "(not set)",
    }
```

- [ ] **Step 5: Register router in app/main.py**

```python
# After existing router imports, add:
from app.api.jira_webhooks import router as jira_webhooks_router
# In create_app(), add:
app.include_router(jira_webhooks_router)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_jira_webhooks.py -v`
Expected: 5 passed

- [ ] **Step 7: Commit**

```bash
git add app/api/jira_webhooks.py app/main.py connectors/jira/service.py tests/test_jira_webhooks.py
git commit -m "feat: Jira webhook FastAPI adapter — replaces Flask Blueprint"
```

---

### Task 2: Docker Compose — Add Missing Services + Scheduler Auth

**Files:**
- Modify: `docker-compose.yml`
- Modify: `services/scheduler/__main__.py`
- Delete: `services/catalog_refresh/`, `services/data_refresh/` (empty dirs)

- [ ] **Step 1: Add missing services to docker-compose.yml**

Add after `telegram-bot` service:

```yaml
  ws-gateway:
    build: .
    command: python -m services.ws_gateway
    volumes:
      - data:/data
    env_file: .env
    environment:
      - DATA_DIR=/data
    depends_on:
      - app
    profiles:
      - full
    restart: unless-stopped

  corporate-memory:
    build: .
    command: python -m services.corporate_memory
    volumes:
      - data:/data
    env_file: .env
    environment:
      - DATA_DIR=/data
    depends_on:
      - app
    profiles:
      - full
    restart: unless-stopped

  session-collector:
    build: .
    command: python -m services.session_collector
    volumes:
      - data:/data
    env_file: .env
    environment:
      - DATA_DIR=/data
    depends_on:
      - app
    profiles:
      - full
    restart: unless-stopped
```

- [ ] **Step 2: Fix scheduler auto-auth**

In `services/scheduler/__main__.py`, add token auto-fetch if `SCHEDULER_API_TOKEN` not set:

```python
# After line 25 (SCHEDULER_API_TOKEN = ...), add:

def _get_auth_token() -> str:
    """Get auth token — use SCHEDULER_API_TOKEN or auto-fetch from API."""
    token = SCHEDULER_API_TOKEN
    if token:
        return token
    # Auto-fetch: call /auth/token with SEED_ADMIN_EMAIL
    admin_email = os.environ.get("SEED_ADMIN_EMAIL", "")
    if not admin_email:
        logger.warning("No SCHEDULER_API_TOKEN or SEED_ADMIN_EMAIL — scheduler calls will be unauthenticated")
        return ""
    try:
        resp = httpx.post(f"{API_URL}/auth/token", json={"email": admin_email}, timeout=10)
        if resp.status_code == 200:
            token = resp.json().get("access_token", "")
            logger.info("Auto-fetched scheduler token for %s", admin_email)
            return token
    except Exception as e:
        logger.warning("Failed to fetch scheduler token: %s", e)
    return ""
```

Update `_call_api` to use `_get_auth_token()`:

```python
def _call_api(endpoint: str, method: str = "POST") -> bool:
    url = f"{API_URL}{endpoint}"
    headers = {}
    token = _get_auth_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    # ... rest unchanged
```

- [ ] **Step 3: Add SEED_ADMIN_EMAIL to scheduler in docker-compose.yml**

```yaml
  scheduler:
    # ... existing config ...
    environment:
      - DATA_DIR=/data
      - API_URL=http://app:8000
      - SEED_ADMIN_EMAIL=${SEED_ADMIN_EMAIL:-}
```

- [ ] **Step 4: Delete empty service directories**

```bash
rm -rf services/catalog_refresh/ services/data_refresh/
```

- [ ] **Step 5: Commit**

```bash
git add docker-compose.yml services/scheduler/__main__.py
git add -A services/catalog_refresh/ services/data_refresh/
git commit -m "feat: add Docker services (ws-gateway, corporate-memory, session-collector) + scheduler auto-auth"
```

---

### Task 3: Dynamic Auth Providers on Login Page

**Files:**
- Modify: `app/web/router.py`

- [ ] **Step 1: Update login_page in router.py**

Replace the hard-coded providers list (lines 152-158):

```python
@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    providers = []
    # Google OAuth — available if credentials configured
    try:
        from app.auth.providers.google import is_available as google_available
        if google_available():
            providers.append({"name": "google", "display_name": "Google", "icon": "google"})
    except Exception:
        pass
    # Password auth — always available
    providers.append({"name": "password", "display_name": "Email & Password", "icon": "key"})
    # Email magic link — available if configured
    try:
        from app.auth.providers.email import is_available as email_available
        if email_available():
            providers.append({"name": "email", "display_name": "Email Link", "icon": "mail"})
    except Exception:
        pass

    ctx = _build_context(request, providers=providers)
    return templates.TemplateResponse(request, "login.html", ctx)
```

- [ ] **Step 2: Verify login page still renders**

Run: `pytest tests/test_api_complete.py::TestWebUI::test_login_page -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add app/web/router.py
git commit -m "feat: dynamic auth provider detection on login page"
```

---

### Task 4: Profiler Auto-Trigger After Sync

**Files:**
- Modify: `app/api/sync.py`
- Modify: `app/api/catalog.py`

- [ ] **Step 1: Add profiler call after orchestrator rebuild in sync.py**

After the orchestrator rebuild line in `_run_sync`, add:

```python
        # Auto-profile synced tables
        try:
            from src.profiler import profile_table, TableInfo, parse_data_description, load_sync_state, load_metrics, get_parquet_path
            from src.db import get_system_db
            from src.repositories.profiles import ProfileRepository
            from pathlib import Path

            data_dir = Path(os.environ.get("DATA_DIR", "./data"))
            extracts_dir = data_dir / "extracts"

            sys_conn = get_system_db()
            try:
                profile_repo = ProfileRepository(sys_conn)
                # Profile each synced table from extract parquets
                for source_name, table_names in views.items():
                    for table_name in table_names[:10]:  # Limit to 10 per sync to avoid timeout
                        pq_path = extracts_dir / source_name / "data" / f"{table_name}.parquet"
                        if not pq_path.exists():
                            continue
                        try:
                            table_info = TableInfo(name=table_name, table_id=table_name)
                            profile = profile_table(table_info, pq_path, [], {}, {})
                            profile_repo.save(table_name, profile)
                        except Exception as pe:
                            print(f"[SYNC] Profile {table_name}: {pe}", file=_sys.stderr, flush=True)
            finally:
                sys_conn.close()
            print(f"[SYNC] Profiler complete", file=_sys.stderr, flush=True)
        except Exception as e:
            print(f"[SYNC] Profiler skipped: {e}", file=_sys.stderr, flush=True)
```

- [ ] **Step 2: Add profile refresh endpoint to catalog.py**

```python
# app/api/catalog.py — add after existing endpoints:

@router.post("/profile/{table_name}/refresh")
async def refresh_profile(
    table_name: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Re-generate profile for a table on demand."""
    import os as _os
    from pathlib import Path as _Path
    from src.profiler import profile_table, TableInfo

    data_dir = _Path(_os.environ.get("DATA_DIR", "./data"))
    extracts_dir = data_dir / "extracts"

    # Find parquet file
    candidates = list(extracts_dir.rglob(f"data/{table_name}.parquet"))
    if not candidates:
        raise HTTPException(status_code=404, detail=f"No parquet for '{table_name}'")

    try:
        table_info = TableInfo(name=table_name, table_id=table_name)
        profile = profile_table(table_info, candidates[0], [], {}, {})
        ProfileRepository(conn).save(table_name, profile)
        return {"status": "ok", "table": table_name, "columns": len(profile.get("columns", {}))}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Profile failed: {e}")
```

- [ ] **Step 3: Commit**

```bash
git add app/api/sync.py app/api/catalog.py
git commit -m "feat: auto-profile after sync + on-demand profile refresh endpoint"
```

---

### Task 5: Final Verification

- [ ] **Step 1: Run full test suite**

```bash
pytest tests/ --ignore=tests/test_cli.py -v
```
Expected: all pass

- [ ] **Step 2: Redeploy to production**

```bash
ssh -i ~/.ssh/google_compute_engine deploy@35.195.96.98 \
  'cd /home/deploy/app && GIT_SSH_COMMAND="ssh -i ~/.ssh/github_deploy -o StrictHostKeyChecking=no" git pull && sudo docker compose build --quiet && sudo docker compose up -d'
```

- [ ] **Step 3: Verify on production**

```bash
# Health
curl http://35.195.96.98:8000/api/health

# Jira webhook health
curl http://35.195.96.98:8000/webhooks/jira/health

# Login page
curl -s http://35.195.96.98:8000/login | grep -o "password\|google\|email"

# Scheduler logs (should show token auto-fetch)
ssh deploy@35.195.96.98 'cd /home/deploy/app && sudo docker compose logs scheduler --tail 5'
```

- [ ] **Step 4: Commit and push**

```bash
git push origin feature/v2-fastapi-duckdb-docker-cli
git push fork feature/v2-fastapi-duckdb-docker-cli
```
