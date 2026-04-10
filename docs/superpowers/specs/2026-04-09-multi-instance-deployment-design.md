# Multi-Instance Deployment & Versioning — Design Spec

## Goal

Make Agnes deployable to 20+ independent customer instances via self-service, with safe versioning that prevents one customer's PR from breaking another's deployment.

## Context

Agnes is an open-source AI Data Analyst platform. Customers (or their AI agents) deploy it as a Docker image on their own infrastructure. Each instance connects to different data sources (Keboola, BigQuery, Jira, custom).

**Key constraints:**
- Customers range from semi-technical to non-technical, assisted by AI agents
- Cloud-agnostic (GCP, AWS, Azure, on-prem, VPS)
- One repo, one Docker image, many instances
- Community PRs must not break existing customers
- AI agent is the primary "installer" and "developer"

---

## 1. Versioning & Release Channels

### CalVer: `YYYY.MM.N`

Format: year.month.sequential-number. Example: `2026.04.1`, `2026.04.2`, `2026.05.1`.

No manual release decisions. Every merge to main is a release.

### Three channels

| Channel | Floating tag | Versioned tag | Source | Who uses it |
|---------|-------------|---------------|--------|-------------|
| **dev** | `:dev` | `:dev-2026.04.N` | Every CI-passing push on any feature branch | Developers, PR testing |
| **stable** | `:stable` | `:stable-2026.04.N` | Every merge to main + CI pass | All production customers |
| **deprecated** | — | `:deprecated-2026.04.N` | Previous stable after breaking change or failed smoke test | Grace period (30 days) |

Every image also gets a `:sha-abc1234` tag for exact commit traceability.

### Tag lifecycle

```
feature branch push → CI ✅ → :dev + :dev-2026.04.N + :sha-abc1234
                         ❌ → nothing pushed

merge to main       → CI ✅ → :stable + :stable-2026.04.N + :sha-abc1234
                         ❌ → merge blocked (CI required)
                                │
                                ▼
                         smoke test on canary VM
                                │
                         ✅ → :stable confirmed
                         ❌ → alert, rollback canary to previous :stable
                              broken build tagged :deprecated-2026.04.N
```

### Version numbering

CalVer `YYYY.MM.N` where N is a global auto-incrementing counter per month across both channels.

Example timeline:
```
Apr 8  feature/foo push     → :dev-2026.04.1
Apr 8  feature/bar push     → :dev-2026.04.2
Apr 8  merge foo to main    → :stable-2026.04.3
Apr 9  feature/baz push     → :dev-2026.04.4
Apr 9  merge bar to main    → :stable-2026.04.5
```

This avoids confusion — version `2026.04.3` exists only once, in one channel.

### Customer pins version

```yaml
# docker-compose.prod.yml

# Auto-update (recommended): always latest stable
image: ghcr.io/keboola/agnes-the-ai-analyst:stable

# Pinned: specific stable release, manual update
image: ghcr.io/keboola/agnes-the-ai-analyst:stable-2026.04.3

# Testing: latest dev
image: ghcr.io/keboola/agnes-the-ai-analyst:dev

# Testing: specific dev build
image: ghcr.io/keboola/agnes-the-ai-analyst:dev-2026.04.2
```

### Main = stable

- `main` branch is always releasable
- Every merge to main triggers a new stable release
- Feature branches are the dev channel
- No promotion pipeline, no manual approval for releases
- Smoke test is a post-deploy safety net, not a gate

---

## 2. Breaking Change Detection

### What is a breaking change

- `_meta` table schema change (add/remove column)
- `_remote_attach` table schema change
- API endpoint removed or response field removed
- DuckDB system schema migration that drops data
- CLI command removed or argument renamed
- `instance.yaml` required key added

### Automated detection in CI

Every PR runs:

1. **Contract tests**: `_meta` and `_remote_attach` schema validation against frozen spec
2. **OpenAPI diff**: Compare PR's `openapi.json` against main's. Flag removed endpoints/fields.
3. **DuckDB schema diff**: Compare table definitions in system.duckdb
4. **Config diff**: Compare `instance.yaml.example` required keys
5. **Full connector matrix**: ALL connectors tested, not just changed ones

If breaking change detected:
- PR gets `BREAKING` label automatically
- Requires 2 reviewers (elevated review)
- Commit message must have `BREAKING:` prefix
- CHANGELOG.md entry with migration guide required
- On merge: previous stable tagged as `:deprecated-YYYY.MM.N`

### Deprecated channel

When a breaking change merges:
1. Previous stable image retagged to `:deprecated-2026.04.N`
2. New build becomes `:stable` + `:2026.04.(N+1)`
3. Health endpoint on deprecated version shows warning:
   ```json
   {"warnings": ["Running deprecated version 2026.04.3. Update to stable."]}
   ```
4. Deprecated images removed from GHCR after 30 days

---

## 3. Smoke Test (Post-Deploy Safety Net)

### What it tests

Automated sequence run on canary VM after every `:stable` deploy:

```
1. GET  /api/health                    → status != "unhealthy"
2. POST /auth/token                    → 200 (valid credentials)
3. GET  /api/catalog/tables            → count > 0
4. POST /api/query {sql: "SELECT 1"}   → 200 + rows
5. POST /api/sync/trigger              → 200
6. (wait 30s)
7. GET  /api/health                    → check no new errors
```

### On failure

1. Alert (GitHub issue + optional webhook)
2. Canary VM rolled back to previous stable: `docker compose pull && docker compose up -d` with previous tag
3. Failed build tagged `:deprecated-YYYY.MM.N`
4. `:stable` tag reverted to previous good build

### Implementation

GitHub Actions workflow triggered after the build-and-push workflow completes:

```yaml
smoke-test:
  needs: build-and-push
  runs-on: ubuntu-latest
  steps:
    - name: Deploy to canary
      run: |
        gcloud compute ssh canary-vm --command="
          cd /opt/agnes &&
          docker compose pull &&
          docker compose up -d"

    - name: Wait for healthy
      run: |
        for i in $(seq 1 30); do
          STATUS=$(curl -sf canary:8000/api/health | jq -r .status)
          [ "$STATUS" != "unhealthy" ] && break
          sleep 10
        done

    - name: Run smoke tests
      run: |
        # auth, catalog, query, sync checks
        ./scripts/smoke-test.sh canary:8000

    - name: Rollback on failure
      if: failure()
      run: |
        # retag and rollback
```

---

## 4. Self-Service Deployment

### Target experience

Customer (or their AI agent) goes from zero to running instance:

```bash
# 1. Get the code
git clone https://github.com/keboola/agnes-the-ai-analyst.git
cd agnes-the-ai-analyst

# 2. Start it
docker compose up -d

# 3. Open browser or use API
# First visit: /setup wizard (no users exist)
# Or headless: curl -X POST localhost:8000/auth/bootstrap ...
```

### Two setup modes

**A) Interactive (browser):**
- First visit when no users exist → redirected to `/setup`
- Step 1: Create admin account (email + password)
- Step 2: Choose data source (Keboola / BigQuery / CSV / Custom)
- Step 3: Enter credentials (token, URL)
- Step 4: Auto-discover and register tables
- Step 5: Trigger first sync
- Done → redirect to dashboard

**B) Headless (AI agent / CLI):**
```bash
# Bootstrap admin
curl -X POST http://localhost:8000/auth/bootstrap \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@company.com","password":"SecurePass123!"}'

# Configure data source
curl -X POST http://localhost:8000/api/admin/configure \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"data_source":"keboola","keboola_token":"...","keboola_url":"..."}'

# Discover and register tables
curl -X POST http://localhost:8000/api/admin/discover-and-register \
  -H "Authorization: Bearer $TOKEN"

# Trigger first sync
curl -X POST http://localhost:8000/api/sync/trigger \
  -H "Authorization: Bearer $TOKEN"
```

Both modes lead to same result. AI agent uses headless.

### Auto-configuration

On first `docker compose up` with no `.env`:
- `JWT_SECRET_KEY` auto-generated and persisted to `/data/state/.jwt_secret`
- `SESSION_SECRET` auto-generated similarly
- App starts in "setup mode" — only `/setup`, `/auth/bootstrap`, and `/api/health` accessible

On first `docker compose up` with `.env` containing `KEBOOLA_STORAGE_TOKEN`:
- Auto-discovers tables from Keboola on first sync
- Skips manual table registration step

### What customer must provide

| Required | Optional |
|----------|----------|
| Server with Docker | Custom domain + TLS |
| Admin email + password | Google OAuth credentials |
| Data source credentials (Keboola token OR BigQuery creds OR CSV files) | Telegram bot token |
| | Jira webhook secret |

### What customer must NOT do

- Edit YAML manually (setup wizard generates `instance.yaml`)
- Generate JWT secret (auto-generated)
- Register tables manually (auto-discovery)
- Understand DuckDB internals

---

## 5. Custom Connectors (Three Tiers)

All tiers produce the same output: `extract.duckdb` with `_meta` table + `data/*.parquet`. Orchestrator treats them identically.

### Tier A: Local mount (fastest, AI-generated)

Customer's AI agent generates a connector. Lives outside Docker image, survives updates.

```
/opt/agnes/
├── docker-compose.yml              ← official image
├── docker-compose.override.yml     ← customer additions
└── custom-connectors/
    └── snowflake/
        ├── extractor.py
        └── requirements.txt
```

```yaml
# docker-compose.override.yml
services:
  app:
    volumes:
      - ./custom-connectors:/app/connectors/custom:ro
```

Orchestrator scans `connectors/custom/*/` in addition to built-in connectors.

**How the AI agent creates one:**
1. Reads CLAUDE.md → understands extract.duckdb contract
2. Reads existing connector as reference (e.g., `connectors/keboola/extractor.py`)
3. Generates `custom-connectors/snowflake/extractor.py`
4. Runs contract test to validate output
5. Done — orchestrator picks it up on next rebuild

**Requirements for this to work:**
- CLAUDE.md must perfectly describe the contract
- Contract test must be runnable standalone
- Existing connectors must be readable as examples
- Clear error messages when contract doesn't match

### Tier B: Standalone container (complex dependencies)

For connectors needing their own runtime (Java, .NET, heavy Python packages).

```yaml
# docker-compose.override.yml
services:
  connector-sap:
    build: ./custom-connectors/sap
    volumes:
      - data:/data
    environment:
      - DATA_DIR=/data
      - SAP_HOST=...
    profiles:
      - extract
```

Connector is its own Docker image. Writes to `/data/extracts/sap/extract.duckdb`. Orchestrator finds it automatically.

### Tier C: Community PR (shared with all)

Connector contributed to main repo via PR. After merge, available in official image for all customers.

```
connectors/
├── keboola/          ← built-in
├── bigquery/         ← built-in
├── jira/             ← built-in
└── snowflake/        ← community contributed
```

**PR requirements:**
- Must pass contract tests
- Must include tests
- Must not modify shared code (orchestrator, API, auth)
- CI runs full connector matrix

---

## 6. CI/CD Pipeline

### On feature branch push

```yaml
ci.yml:
  - tests (all 654+)
  - contract tests (all connectors)
  - docker build
  - push :dev + :dev-sha-xxx to GHCR
```

### On merge to main

```yaml
release.yml:
  - tests (all)
  - contract tests (all connectors)
  - breaking change detection (OpenAPI diff, schema diff)
  - docker build
  - push :stable + :YYYY.MM.N + :sha-xxx to GHCR
  - trigger smoke test on canary

smoke-test.yml (triggered):
  - deploy to canary VM
  - run smoke test sequence
  - on failure: rollback canary, tag build as deprecated, create alert
```

### On PR

```yaml
pr-check.yml:
  - tests
  - contract tests
  - breaking change detection
  - label PR: "BREAKING" if detected
  - require 2 reviewers if breaking
```

---

## 7. Infrastructure (Cloud-Agnostic)

### Primary: Docker Compose

Works everywhere Docker runs. This is the default and only required deployment method.

```bash
git clone https://github.com/keboola/agnes-the-ai-analyst.git
cd agnes-the-ai-analyst
docker compose up -d
```

### Optional: Terraform (GCP)

For automated provisioning. Lives in `infra/` with GCS remote state backend.

```bash
cd infra
terraform workspace new customer-name
terraform apply -var-file=instances/customer-name.tfvars
```

Creates VM, installs Docker, clones repo, generates `.env` and `instance.yaml`, starts Docker Compose.

### Optional: Caddy TLS

Production profile adds Caddy reverse proxy with automatic Let's Encrypt:

```bash
DOMAIN=data.customer.com docker compose --profile production up -d
```

### Directory layout on customer server

```
/opt/agnes/                           ← git clone
├── docker-compose.yml                ← official
├── docker-compose.prod.yml           ← GHCR images
├── docker-compose.override.yml       ← customer customizations
├── .env                              ← secrets (gitignored)
├── config/
│   └── instance.yaml                 ← generated by setup wizard
├── custom-connectors/                ← Tier A connectors
│   └── snowflake/
└── Caddyfile                         ← TLS config

/data/                                ← Docker volume (persistent)
├── state/system.duckdb               ← users, registry, sync state
├── analytics/server.duckdb           ← views into extracts
└── extracts/                         ← per-source data
    ├── keboola/extract.duckdb
    ├── bigquery/extract.duckdb
    └── snowflake/extract.duckdb      ← from custom connector
```

---

## 8. AI Agent as Primary Installer

CLAUDE.md and documentation must be optimized for AI agent consumption:

### CLAUDE.md requirements
- Complete extract.duckdb contract with exact SQL for `_meta` and `_remote_attach`
- Step-by-step setup instructions with exact curl commands
- Existing connectors as reference for AI-generated new ones
- Clear error messages explaining what went wrong and how to fix

### API requirements
- All setup operations available as API calls (not just UI)
- Self-describing error messages: `"Missing KEBOOLA_STORAGE_TOKEN. Set it in .env or pass via /api/admin/configure"`
- `/api/health` returns structured diagnostics AI agent can parse
- `/api/admin/configure` accepts data source config without file editing

### Documentation requirements
- Machine-readable (no screenshots, no "click here")
- Every manual step has an equivalent API/CLI command
- QUICKSTART.md optimized for copy-paste by AI agent

---

## 9. What Needs to Be Built

### Must have (blocks multi-instance)

| # | What | Effort |
|---|------|--------|
| 1 | CalVer auto-tagging in CI (release.yml) | 1 day |
| 2 | Smoke test script + CI workflow | 1 day |
| 3 | Breaking change detection in CI (OpenAPI diff, contract diff) | 2 days |
| 4 | `/setup` wizard (web) + `/api/admin/configure` (headless) | 3 days |
| 5 | Auto-generate JWT_SECRET_KEY on first start | 0.5 day |
| 6 | Auto-discovery for Keboola tables on first sync | 1 day |
| 7 | Custom connector mount support in orchestrator | 1 day |
| 8 | `CHANGELOG.md` + release notes template | 0.5 day |
| 9 | Health endpoint version + channel info | 0.5 day |

### Should have (improves experience)

| # | What | Effort |
|---|------|--------|
| 10 | Deprecated version warning in health endpoint | 0.5 day |
| 11 | `/api/admin/discover-and-register` auto-discovery endpoint | 1 day |
| 12 | Standalone container connector example (Tier B) | 0.5 day |
| 13 | CLAUDE.md optimization for AI agent setup | 1 day |
| 14 | Terraform module refactor for multi-workspace | 1 day |

### Nice to have (future)

| # | What |
|---|------|
| 15 | Community connector contribution guide |
| 16 | Instance health dashboard (central monitoring) |
| 17 | Automated backup (GCP disk snapshots) |
| 18 | Usage analytics (opt-in telemetry) |

---

## Non-Goals

- Multi-tenancy in single process (each customer = separate instance)
- Kubernetes/Helm (Docker Compose is sufficient for target scale)
- Paid tier / license keys (open-source, monetization TBD)
- GUI for connector development (AI agent + CLAUDE.md is sufficient)
