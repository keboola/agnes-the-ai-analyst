# Deployment Guide

Agnes supports two deployment paths. Pick the one that matches your use case.

## 1. Terraform — managed, multi-customer (recommended)

For Keboola-operated deployments and anyone running Agnes for multiple customers on GCP.

**Follow:** [`ONBOARDING.md`](ONBOARDING.md)

Highlights:
- Per-customer GCP project + private infra repo cloned from [`keboola/agnes-infra-template`](https://github.com/keboola/agnes-infra-template)
- Reusable Terraform module `infra/modules/customer-instance` (versioned — `infra-vX.Y.Z` tags)
- Prod + optional branch-aware dev VMs
- Persistent SSD data disk with daily snapshots
- Secret Manager for tokens (no plaintext in VM metadata)
- OS Login for SSH, dedicated VM service account with scoped `secretAccessor`
- Cron-based auto-upgrade (pulls `:stable` image digest every 5 min)
- Caddy TLS with corporate-CA or self-managed certs mounted from `/data/state/certs`; daily auto-rotation from a URL (`TLS_FULLCHAIN_URL`) with zero-downtime `SIGUSR1` reload
- Uptime check + alert policy per VM (wire a notification channel to be paged)
- CI/CD in the private repo: PR → `terraform plan`, merge to main → `apply-dev` auto, `apply-prod` gated by reviewer
- First-boot bootstrap via `POST /auth/bootstrap`

Target onboarding time: **< 1 hour** per customer.

## 2. Docker Compose — OSS self-host

For running Agnes on your own VM / bare metal without Terraform. You're responsible for provisioning and maintenance.

### Prerequisites

- Ubuntu 24.04 (or any Linux with Docker)
- 2 vCPU, 2 GB RAM, 30 GB SSD minimum
- Docker Engine + Compose plugin
- Public IP with ports 80/443 (if using Caddy TLS) or 8000 (plain HTTP) open
- Data-source credentials (e.g., Keboola Storage token)

### Steps

1. Clone the Agnes repository:

   ```bash
   git clone https://github.com/keboola/agnes-the-ai-analyst.git /opt/agnes
   cd /opt/agnes
   ```

2. Create `.env`:

   ```bash
   cat > .env <<'EOF'
   JWT_SECRET_KEY=$(openssl rand -hex 32)
   DATA_DIR=/data
   DATA_SOURCE=keboola
   KEBOOLA_STORAGE_TOKEN=<your-token>
   KEBOOLA_STACK_URL=<your-stack-url>
   SEED_ADMIN_EMAIL=<your-email>
   LOG_LEVEL=info
   AGNES_TAG=stable
   EOF
   chmod 600 .env
   ```

3. Mount a persistent disk at `/data` (optional but recommended — survives host rebuild). If you do, use the overlay:

   ```bash
   docker compose \
       -f docker-compose.yml \
       -f docker-compose.prod.yml \
       -f docker-compose.host-mount.yml \
       up -d
   ```

   Without a persistent disk (data on Docker named volume, tied to boot disk):

   ```bash
   docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
   ```

4. Bootstrap your admin password via `POST /auth/bootstrap`:

   ```bash
   curl -X POST http://<host>:8000/auth/bootstrap \
       -H "Content-Type: application/json" \
       -d '{"email":"<your-email>","password":"<strong-password>"}'
   ```

5. Open `http://<host>:8000/login` and sign in.

### TLS (optional)

Caddy runs as the TLS terminator. It reads certs from `/data/state/certs/{fullchain,privkey}.pem` bind-mounted into the container. Two provisioning modes:

**A. Public internet (Let's Encrypt)** — for this path, override the `Caddyfile` to drop the `tls` directive (so Caddy auto-issues) and skip steps below. Not covered here anymore; see git history prior to the `feat(tls)` change if you need the ACME flow.

**B. Corporate CA / self-managed certs** (recommended, and what the infra repo ships):

Two bring-up flows, picked by whether `TLS_PRIVKEY_URL` is set in `.env`:

- **On-VM gen** (preferred for new deployments): leave `TLS_PRIVKEY_URL` empty. On first run, `agnes-tls-rotate.sh` generates an RSA-2048 key + CSR directly into `/data/state/certs/` using the subject string from `TLS_CSR_SUBJECT`. The key never leaves the host; the CSR (`/data/state/certs/cert.csr`) is what you submit to your corporate PKI. Until the CA signs and publishes, rotate falls back to a 30-day self-signed cert against the same key so Caddy can serve :443.
- **Pre-provisioned key** (legacy / VM-replace-resilient): set `TLS_PRIVKEY_URL=sm://<secret>` (or any supported scheme). Seed the key out-of-band before first rotate. Same real-cert fetch + self-signed fallback applies.

Both modes converge: once the CA publishes the signed chain at `TLS_FULLCHAIN_URL`, the daily rotate tick atomically swaps the fullchain in place and `SIGUSR1`-reloads Caddy. Zero key churn, zero downtime, no reload when the URL content hasn't moved.

1. Set the required env vars in `.env`:
   ```
   DOMAIN=agnes.example.com
   TLS_FULLCHAIN_URL=https://your-ca.example.com/agnes/fullchain.pem
   TLS_PRIVKEY_URL=            # empty → on-VM gen; or sm://<secret>
   TLS_CSR_SUBJECT=/C=…/ST=…/L=…/O=…/CN=agnes.example.com
   ```
2. Start with the `tls` profile + overlay (`docker-compose.tls.yml` closes host `:8000` so all traffic enters via `:443`):
   ```bash
   docker compose \
       -f docker-compose.yml \
       -f docker-compose.prod.yml \
       -f docker-compose.tls.yml \
       --profile tls up -d
   ```
3. Grab the CSR if you used on-VM gen:
   ```bash
   sudo cat /data/state/certs/cert.csr
   ```
   Submit to your corporate PKI. While waiting, Caddy is already up on :443 with the self-signed fallback.

#### Automatic rotation

`scripts/ops/agnes-tls-rotate.sh` is the single entry point — it handles fetch, self-signed fallback, auto-generation on missing key, atomic cert swap, and Caddy reload. Env vars it reads:

| Var | Required | Schemes | Notes |
|---|---|---|---|
| `DOMAIN` | yes | — | The hostname Caddy serves + the CN in auto-generated CSRs. |
| `TLS_FULLCHAIN_URL` | yes | `https://`, `sm://<secret>`, `gs://<obj>`, `file://` | Polled daily; rotate only reloads Caddy when the bytes change. |
| `TLS_PRIVKEY_URL` | optional | same | Empty activates on-VM gen. Set to pre-provisioned scheme (e.g. `sm://`) for VM-replace resilience. |
| `TLS_CSR_SUBJECT` | optional | — | Stamped on auto-generated CSRs. Defaults to `/CN=<DOMAIN>` if unset. Example: `/C=US/ST=Illinois/L=Chicago/O=Your Org/CN=agnes.example.com`. |

`scripts/tls-fetch.sh` at `/usr/local/bin/tls-fetch.sh` is required (generic URL fetcher used by rotate). On infra-repo-managed VMs, both scripts are installed by `startup.sh` and fired via a daily systemd timer; for manual compose deployments, copy them under `/usr/local/bin/` and wire a systemd timer (`OnBootSec=10min`, `OnUnitActiveSec=24h`, `Persistent=true`).

### Upgrades (manual)

```bash
cd /opt/agnes
git pull
docker compose -f docker-compose.yml -f docker-compose.prod.yml pull
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

Or set up a cron job — see `infra/modules/customer-instance/startup-script.sh.tpl` for the reference implementation.

### Health checks & external monitoring

Two health endpoints serve different audiences:

| Endpoint | Auth | Response | Use for |
|---|---|---|---|
| `GET /api/health` | None | `{"status": "ok"}` | Load balancers, Docker `healthcheck`, uptime pings |
| `GET /api/health/detailed` | Bearer token | `{"status", "version", "services": {...}}` | Dashboards, alerting rules, `da diagnose`/`da status` CLI |

The Docker Compose `healthcheck` uses the minimal endpoint (`curl -sf http://localhost:8000/api/health`). For external monitoring tools (Datadog, Prometheus, UptimeRobot, etc.) that need service-level detail (DuckDB status, sync freshness, user count), point them at `/api/health/detailed` with an `Authorization: Bearer <token>` header. Any authenticated user can call it; a personal access token (`da admin create-pat`) works well for service accounts.

### Scheduler tuning

The scheduler sidecar (`services/scheduler/__main__.py`) fires periodic
HTTP calls against the main app. Job cadences are configurable via env
vars on the scheduler container:

| Env var                            | Default | Purpose                                       |
| ---------------------------------- | ------- | --------------------------------------------- |
| `SCHEDULER_DATA_REFRESH_INTERVAL`  | `900`   | seconds between `POST /api/sync/trigger`      |
| `SCHEDULER_HEALTH_CHECK_INTERVAL`  | `300`   | seconds between `GET /api/health`             |
| `SCHEDULER_SCRIPT_RUN_INTERVAL`    | `60`    | seconds between `POST /api/scripts/run-due`   |
| `SCHEDULER_TICK_SECONDS`           | `30`    | loop polling cadence; must be ≤ smallest interval above |

`/api/sync/trigger` walks `table_registry`; tables with a per-row
`sync_schedule` (`every Nm` / `every Nh` / `daily HH:MM[,...]`) are
filtered to only those due for sync since their last run. Tables without
a schedule continue to run on every tick. The marketplace job runs at
`daily 03:00` UTC and is not currently env-tunable.

`/api/scripts/run-due` walks `script_registry` and runs each deployed
script whose `schedule` says it is due. Scripts in the `running` state
are skipped on subsequent ticks until the previous run writes a terminal
status. The endpoint requires admin auth (the sidecar's
`SCHEDULER_API_TOKEN` resolves to a synthetic Admin user).

#### Caveats

- **Sub-minute env values are quantized to 1 minute.** The schedule grammar
  has minute-level resolution, so `SCHEDULER_DATA_REFRESH_INTERVAL=30`
  silently becomes a 1-minute cadence rather than 30 seconds. Pick values
  ≥ 60 to avoid surprise.
- **A crashed BackgroundTask can leave a script stuck in `last_status='running'`.**
  The next sidecar tick will skip the stuck script forever. Recovery is
  manual: open a DuckDB shell on `system.duckdb` and run
  `UPDATE script_registry SET last_status = NULL WHERE id = '<id>';`
  Auto-recovery via max-runtime detection is intentionally out of scope
  for v0; revisit if it happens in practice.

## Which path should I pick?

| | Terraform | Docker Compose |
|---|---|---|
| Setup time | ~45 min first customer, ~15 min each subsequent | ~30 min |
| Infra-as-Code | Full (all resources in git) | Partial (compose.yml only) |
| Secret storage | GCP Secret Manager | `.env` file on host |
| Upgrades | Auto via cron, gated prod apply | Manual `docker compose pull` |
| Backups | Daily GCP snapshots, 30-day retention | You set up yourself |
| Monitoring / alerts | GCP Uptime Checks + alert policy | You set up yourself |
| TLS | Caddy + corp cert, auto-rotated from URL | Caddy + corp cert, manual or user-scripted rotation |
| Best for | Multi-tenant SaaS, production | Single-instance self-host, learning |

## Related documentation

- [`ONBOARDING.md`](ONBOARDING.md) — end-to-end Terraform onboarding checklist
- [`CONFIGURATION.md`](CONFIGURATION.md) — `instance.yaml`, env vars, per-instance config
- [`architecture.md`](architecture.md) — internal architecture (orchestrator, extractors, DB layout)
- [`QUICKSTART.md`](QUICKSTART.md) — local development setup
- [`superpowers/specs/2026-04-21-multi-customer-deployment-spec.md`](superpowers/specs/2026-04-21-multi-customer-deployment-spec.md) — design rationale for the multi-customer model
