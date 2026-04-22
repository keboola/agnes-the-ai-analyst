# Deployment & Multi-Instance Readiness Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the platform deployable to N customer instances with minimal manual effort.

**Architecture:** Docker image on GHCR + per-instance config (instance.yaml + .env) + Terraform provisioning. One image, many instances.

**Tech Stack:** Docker, Terraform (GCP), GitHub Actions, Caddy (TLS proxy)

**Source:** Deployment readiness + multi-instance architecture reviews 2026-04-09 (findings C5-C7, I4-I9)

---

## File Map

| File | Responsibility | Tasks |
|------|---------------|-------|
| `config/.env.template` | Complete env var reference | 1 |
| `docker-compose.yml` | Add restart policy, config mount, image ref, Caddy proxy | 2, 3 |
| `docker-compose.prod.yml` | Production override with GHCR image + Caddy | 2, 3 |
| `.github/workflows/deploy.yml` | Image versioning with SHA tag | 4 |
| `infra/main.tf` | Remote state backend, instance.yaml generation | 5 |
| `services/telegram_bot/config.py` | Fix hardcoded paths | 6 |
| `src/profiler.py` | Fix PROFILER_DATA_DIR | 6 |
| `docs/DEPLOYMENT.md` | Update for multi-instance | 7 |

---

### Task 1: Complete .env.template with all env vars

The template lists only 8 of ~15 needed variables.

**Files:**
- Modify: `config/.env.template`

- [ ] **Step 1: Rewrite .env.template**

```bash
# Agnes AI Data Analyst - Environment Variables
# =============================================
# Copy to .env: cp config/.env.template .env
# .env is gitignored - NEVER commit it.

# ── REQUIRED ────────────────────────────────────────
JWT_SECRET_KEY=              # python -c "import secrets; print(secrets.token_hex(32))"
SESSION_SECRET=              # python -c "import secrets; print(secrets.token_hex(32))"

# ── GOOGLE OAUTH (required for Google login) ────────
# GOOGLE_CLIENT_ID=
# GOOGLE_CLIENT_SECRET=

# ── KEBOOLA (required for Keboola data source) ──────
# KEBOOLA_STORAGE_TOKEN=
# KEBOOLA_STACK_URL=https://connection.keboola.com

# ── BIGQUERY (required for BigQuery data source) ─────
# BIGQUERY_PROJECT=
# BIGQUERY_LOCATION=us

# ── BOOTSTRAP (first deploy only) ───────────────────
# SEED_ADMIN_EMAIL=admin@example.com

# ── EMAIL / SMTP (required for magic link auth) ─────
# SMTP_HOST=smtp.gmail.com
# SMTP_PORT=587
# SMTP_USER=
# SMTP_PASSWORD=

# ── OPTIONAL SERVICES ───────────────────────────────
# TELEGRAM_BOT_TOKEN=
# JIRA_WEBHOOK_SECRET=
# JIRA_API_TOKEN=
# ANTHROPIC_API_KEY=
# LLM_API_KEY=

# ── DESKTOP APP ─────────────────────────────────────
# DESKTOP_JWT_SECRET=       # Separate secret for desktop app tokens

# ── DEPLOYMENT ──────────────────────────────────────
# DATA_DIR=/data            # Default: /data in Docker, ./data locally
# LOG_LEVEL=info            # debug, info, warning, error
# CORS_ORIGINS=http://localhost:3000,http://localhost:8000
```

- [ ] **Step 2: Commit**

```bash
git add config/.env.template
git commit -m "docs: complete .env.template with all 20+ env vars"
```

---

### Task 2: Fix docker-compose for production (I7, I5, I8)

Add restart policy to app, config volume mount, and GHCR image reference.

**Files:**
- Modify: `docker-compose.yml`
- Create: `docker-compose.prod.yml` (production override)

- [ ] **Step 1: Add restart policy and config mount to docker-compose.yml**

In `docker-compose.yml`, add to the `app` service:

```yaml
  app:
    build: .
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000
    ports:
      - "8000:8000"
    volumes:
      - data:/data
      - ./config:/app/config:ro
    env_file: .env
    environment:
      - DATA_DIR=/data
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-sf", "http://localhost:8000/api/health"]
      interval: 30s
      timeout: 5s
      retries: 3
```

Key changes: `restart: unless-stopped` added, `./config:/app/config:ro` volume mount added.

- [ ] **Step 2: Create docker-compose.prod.yml**

```yaml
# Production override — uses pre-built GHCR image instead of local build.
# Usage: docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
services:
  app:
    image: ghcr.io/keboola/agnes-the-ai-analyst:latest
    build: !reset null

  scheduler:
    image: ghcr.io/keboola/agnes-the-ai-analyst:latest
    build: !reset null

  extract:
    image: ghcr.io/keboola/agnes-the-ai-analyst:latest
    build: !reset null

  telegram-bot:
    image: ghcr.io/keboola/agnes-the-ai-analyst:latest
    build: !reset null

  ws-gateway:
    image: ghcr.io/keboola/agnes-the-ai-analyst:latest
    build: !reset null

  corporate-memory:
    image: ghcr.io/keboola/agnes-the-ai-analyst:latest
    build: !reset null

  session-collector:
    image: ghcr.io/keboola/agnes-the-ai-analyst:latest
    build: !reset null
```

- [ ] **Step 3: Commit**

```bash
git add docker-compose.yml docker-compose.prod.yml
git commit -m "feat: add restart policy, config mount, production compose override with GHCR images"
```

---

### Task 3: Add Caddy reverse proxy for TLS (I4)

No HTTPS in Docker Compose — data transits in plaintext.

**Files:**
- Create: `Caddyfile`
- Modify: `docker-compose.yml` (add caddy service)

- [ ] **Step 1: Create Caddyfile**

```
{$DOMAIN:localhost} {
    reverse_proxy app:8000
}
```

- [ ] **Step 2: Add Caddy service to docker-compose.yml**

Add to services section:

```yaml
  caddy:
    image: caddy:2-alpine
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
      - caddy_config:/config
    environment:
      - DOMAIN=${DOMAIN:-localhost}
    depends_on:
      app:
        condition: service_healthy
    restart: unless-stopped
    profiles:
      - production
```

Add volumes:
```yaml
volumes:
  data:
  caddy_data:
  caddy_config:
```

- [ ] **Step 3: Update DEPLOYMENT.md**

Add section:

```markdown
### HTTPS with Caddy (production)

Set `DOMAIN=data.yourcompany.com` in `.env`, then:

```bash
docker compose --profile production up -d
```

Caddy automatically provisions Let's Encrypt TLS certificates.
```

- [ ] **Step 4: Commit**

```bash
git add Caddyfile docker-compose.yml docs/DEPLOYMENT.md
git commit -m "feat: add Caddy reverse proxy for automatic HTTPS in production"
```

---

### Task 4: Add Docker image versioning with commit SHA (C7)

Images are only tagged `:latest` — no versioning, no rollback.

**Files:**
- Modify: `.github/workflows/deploy.yml`

- [ ] **Step 1: Update image tagging**

In `.github/workflows/deploy.yml`, replace the build-and-push step:

```yaml
      - name: Build and push
        uses: docker/build-push-action@v7
        with:
          push: true
          tags: |
            ghcr.io/${{ github.repository }}:latest
            ghcr.io/${{ github.repository }}:${{ github.sha }}
```

- [ ] **Step 2: Commit**

```bash
git add -f .github/workflows/deploy.yml
git commit -m "feat: tag Docker images with commit SHA for versioning and rollback"
```

---

### Task 5: Add Terraform remote state backend (I6)

Local tfstate blocks multi-operator and multi-instance Terraform.

**Files:**
- Modify: `infra/main.tf`
- Modify: `infra/variables.tf`

- [ ] **Step 1: Add GCS backend to main.tf**

In `infra/main.tf`, inside the `terraform {}` block:

```hcl
terraform {
  required_version = ">= 1.5"

  backend "gcs" {
    bucket = "agnes-terraform-state"
    prefix = "instances"
  }

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.0"
    }
  }
}
```

- [ ] **Step 2: Add instance.yaml generation to startup script**

In `infra/main.tf`, in the `startup_script` local, after the `.env` generation:

```bash
    echo "=== Creating instance.yaml ==="
    cat > "$APP_DIR/config/instance.yaml" << 'YAMLEOF'
    instance:
      name: "${var.instance_name}"
      subtitle: "Data Analytics Platform"
    server:
      host: "${google_compute_address.data_analyst.address}"
      hostname: "${var.domain != "" ? var.domain : google_compute_address.data_analyst.address}"
      port: 8000
    auth:
      allowed_domain: "${var.admin_email != "" ? join("", [split("@", var.admin_email)[1]]) : ""}"
    data_source:
      type: "${var.keboola_token != "" ? "keboola" : "local"}"
    YAMLEOF
    sed -i 's/^    //' "$APP_DIR/config/instance.yaml"
```

- [ ] **Step 3: Update repo URL in startup script**

Replace line 73 `git clone https://github.com/padak/tmp_oss.git` with:
```bash
    git clone https://github.com/keboola/agnes-the-ai-analyst.git "$APP_DIR"
```

And line 75 `git checkout feature/v2-fastapi-duckdb-docker-cli` with:
```bash
    # main branch is default, no checkout needed
```

- [ ] **Step 4: Commit**

```bash
git add infra/main.tf infra/variables.tf
git commit -m "feat: add Terraform GCS remote state, instance.yaml generation, update repo URL"
```

---

### Task 6: Fix hardcoded paths in services (I9)

telegram_bot and profiler use hardcoded `/data/...` paths instead of `DATA_DIR`.

**Files:**
- Modify: `services/telegram_bot/config.py:14`
- Modify: `src/profiler.py:87`
- Modify: `services/telegram_bot/dispatch.py:17`

- [ ] **Step 1: Fix telegram_bot config**

In `services/telegram_bot/config.py`, replace line 14:

```python
NOTIFICATIONS_DIR = os.path.join(os.environ.get("DATA_DIR", "/data"), "notifications")
```

- [ ] **Step 2: Fix profiler**

In `src/profiler.py`, replace line 87:

```python
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data")) / "src_data"
```

Remove `PROFILER_DATA_DIR` reference — use standard `DATA_DIR` like everywhere else.

- [ ] **Step 3: Fix dispatch.py**

In `services/telegram_bot/dispatch.py`, replace line 17:

```python
WS_GATEWAY_SOCKET_PATH = os.environ.get("WS_GATEWAY_SOCKET", "/run/ws-gateway/ws.sock")
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/ -q --tb=short`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add services/telegram_bot/config.py services/telegram_bot/dispatch.py src/profiler.py
git commit -m "fix: use DATA_DIR env var everywhere — remove hardcoded /data paths"
```

---

### Task 7: Update DEPLOYMENT.md for multi-instance

Add production deployment with GHCR images, Caddy TLS, and multi-instance guidance.

**Files:**
- Modify: `docs/DEPLOYMENT.md`

- [ ] **Step 1: Add sections to DEPLOYMENT.md**

Add these sections:

**Production with GHCR images:**
```markdown
### Production Deployment (pre-built images)

Instead of building locally, pull from GitHub Container Registry:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

Pin to a specific version:
```bash
# In docker-compose.prod.yml, change :latest to :COMMIT_SHA
image: ghcr.io/keboola/agnes-the-ai-analyst:abc1234
```
```

**Multi-instance:**
```markdown
## Multi-Instance Deployment

Each customer gets a separate VM with isolated data and config.

1. Copy `infra/terraform.tfvars.example` to `infra/instances/customer-name.tfvars`
2. Fill in customer-specific values
3. Apply: `cd infra && terraform workspace new customer-name && terraform apply -var-file=instances/customer-name.tfvars`
4. SSH in and create `config/instance.yaml` from `config/instance.yaml.example`
5. Start: `docker compose -f docker-compose.yml -f docker-compose.prod.yml --profile production up -d`
6. Bootstrap: `curl -X POST http://IP:8000/auth/bootstrap -d '{"email":"admin@customer.com"}'`
```

**Update/rollback:**
```markdown
## Updating an Instance

```bash
# Pull latest image
docker compose -f docker-compose.yml -f docker-compose.prod.yml pull

# Restart with new image
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d

# Rollback to specific version
# Edit docker-compose.prod.yml: change :latest to :PREVIOUS_SHA
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```
```

- [ ] **Step 2: Commit**

```bash
git add docs/DEPLOYMENT.md
git commit -m "docs: add multi-instance deployment, GHCR images, update/rollback procedures"
```

---

## Execution Order

Sequential recommended (some tasks depend on earlier ones):

1. **Task 1** — .env.template (no deps)
2. **Task 2** — docker-compose fixes (no deps)
3. **Task 3** — Caddy TLS (depends on Task 2)
4. **Task 4** — image versioning (no deps)
5. **Task 5** — Terraform remote state (no deps)
6. **Task 6** — hardcoded paths (no deps)
7. **Task 7** — documentation (depends on all above)

Tasks 1, 2, 4, 5, 6 can run in parallel.

**Verification after all tasks:**

```bash
# Tests still pass
pytest tests/ -v --tb=short

# Docker builds
docker compose build

# Production compose validates
docker compose -f docker-compose.yml -f docker-compose.prod.yml config
```
