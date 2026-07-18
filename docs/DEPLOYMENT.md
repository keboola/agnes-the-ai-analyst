> New: [docs/PLATFORM_SETUP.md](./PLATFORM_SETUP.md) is the consolidated operator playbook. This doc covers a focused subset; check the playbook first.

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
   cat > .env <<EOF
   JWT_SECRET_KEY=$(openssl rand -hex 32)
   AGNES_VAULT_KEY=$(openssl rand -base64 32 | tr '+/' '-_')
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

   `AGNES_VAULT_KEY` (a Fernet key: 32 random bytes, URL-safe base64) encrypts
   admin-stored secrets — datasource credentials, Slack tokens, MCP secrets —
   at rest. Without it those admin endpoints refuse writes
   (`409 vault_key_not_configured`). Keep the key stable and include it in your
   backups: losing it makes every previously stored secret undecryptable.
   (The Terraform module generates and persists this key automatically; this
   manual step matters only on self-provisioned hosts.)

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
| `GET /api/health/detailed` | Bearer token | `{"status", "version", "services": {...}}` | Dashboards, alerting rules, `agnes diagnose`/`agnes status` CLI |

The Docker Compose `healthcheck` uses the minimal endpoint (`curl -sf http://localhost:8000/api/health`). For external monitoring tools (Datadog, Prometheus, UptimeRobot, etc.) that need service-level detail (DuckDB status, sync freshness, user count), point them at `/api/health/detailed` with an `Authorization: Bearer <token>` header. Any authenticated user can call it; a personal access token (`agnes admin create-pat`) works well for service accounts.

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

- **Schedule quantization rounds up.** The schedule grammar has minute-
  level resolution. Non-multiples of 60 seconds round UP to the next
  minute (`SCHEDULER_DATA_REFRESH_INTERVAL=90` → `every 2m`, not `every 1m`)
  so a job never fires more often than configured. Sub-minute values
  clamp to `every 1m`. Use multiples of 60 for predictable cadence.
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

## Multi-process

`AGNES_ROLE` (env or `instance.yaml::deployment.role`) selects which planes a
process serves: `api`, `gateway`, `worker`, or `all` (default — today's
single-process behavior, no new requirements).

Any multi-process topology (role split, or `UVICORN_WORKERS > 1`) must set:

- `DATABASE_URL` (or `database.backend`) — Postgres app-state,
- `JWT_SECRET_KEY` and `SESSION_SECRET` — explicit shared secrets,
- `coordination.backend: redis` — shared coordination (see the m-tier profile).

The app refuses to start otherwise, naming what is missing. Probes:
`/healthz` (liveness), `/readyz` (readiness — background write-canary with
hysteresis; point LB health checks here). `/api/health` is unchanged.
Try it: `./scripts/dev/mtier-smoke.sh`.

The `worker` role runs the durable job queue's worker runtime instead of
handling HTTP traffic: two lanes — heavy (`data-refresh`, `jira-refresh`)
and light (`marketplaces-sync`, `session-collector`, `corporate-memory`)
— each claim rows off the `jobs` table with a lease + retry lifecycle and
run the corresponding handler. The scheduler now enqueues these kinds via
`POST /api/jobs` and returns immediately instead of calling their HTTP
endpoint and waiting; a handful of cheap or not-yet-migrated rows still
call their endpoint synchronously (full split in
[`jobs-classification.md`](jobs-classification.md)). Inspect the queue
with `agnes admin jobs list` (`--status`/`--kind` to filter) or
`agnes admin jobs show <job_id>` for one row.

### Coordination backend

`coordination.backend` (`instance.yaml`) / `AGNES_COORDINATION_BACKEND`
(env) selects the `CoordinationBackend` implementation everything above
(and several other cross-process concerns) rides on: `memory` (default)
or `redis`, pointed at `redis.url` / `AGNES_REDIS_URL`. `memory` is
process-local — fine for `all`-role, single-process deployments, and it's
what every non-multi-process topology uses today. `redis` is required as
soon as any process is split by role or `UVICORN_WORKERS > 1` — it is
itself one of the three conditions `is_multi_process()` checks (alongside
`DATABASE_URL`/`database.backend` and `UVICORN_WORKERS > 1`), so
configuring it alone is enough to opt a topology into the multi-process
startup guards even before an actual role split.

In `redis` mode the backend carries:

- WS auth tickets (chat stream/join and the admin tail route) — single-use,
  60 s TTL, visible to whichever replica the client's next request lands on;
- leader leases for the Slack Socket Mode connection, the Telegram
  long-poll loop, and the paused-sandbox TTL sweep — exactly one replica
  runs each singleton consumer at a time;
- shared per-IP auth-endpoint rate limits and chat per-user hourly
  message / daily token quotas — counted once across all replicas instead
  of once per replica;
- cache-invalidation pub/sub for the v2 catalog/schema/sample TTL caches —
  every api replica drops its own stale entries on a registry change;
- operational auth codes — CLI-auth login codes and Slack binding codes —
  as `redis` KV instead of `operational.duckdb` reads/writes; and
- `.env_overlay` token reload — a rotated marketplace/template PAT or
  chat-sandbox key is written once and every api/worker/gateway replica
  picks it up via a pub/sub event (plus a periodic re-read, belt-and-braces,
  every `AGNES_STATE_CHECKPOINT_INTERVAL_S`, default 300 s).

Configuring `redis` mode also **removes `operational.duckdb` from
multi-process topologies** — it was one of only two files every replica
still opened read/write; with `redis` mode, operational codes live in
Redis instead, and no replica needs to touch that file. (`memory` mode is
unaffected: `operational.duckdb` remains the storage for CLI-auth/Slack
binding codes there.)

**Disposability invariant.** A single non-HA Redis is the supported
shape — no cluster, no sentinel, no persistence requirement. Every
consumer above is designed to recover cleanly from a Redis `FLUSHALL` (or
the instance being replaced outright): leases are re-acquired within their
TTL by whichever replica asks next; WS tickets are simply re-minted on the
client's next request; rate-limit and quota counters reset to zero (a
brief extra-generous window, never a false 429); operational codes are
gone, so an in-flight login/bind flow must be re-run from the start; and
`.env_overlay` values go stale for at most the periodic re-read interval
(`AGNES_STATE_CHECKPOINT_INTERVAL_S`, default 300 s) before every replica
converges again. Nothing durable — app state, table registry, secrets —
lives in Redis; it is purely a coordination cache. The redis-py client is
configured with bounded socket connect/read timeouts, so a hung or
unreachable Redis surfaces as a prompt `CoordinationUnavailable` (which
callers already handle — e.g. a graceful WS close with code 4503) instead
of blocking a lease-heartbeat thread indefinitely.

### Metrics (Prometheus)

Every role process exposes `GET /metrics` (`app/observability/metrics.py`)
in the standard Prometheus text exposition format — **unauthenticated,
internal-scrape-only**, the same posture as `/healthz`/`/readyz`. Put it
behind the same TLS-terminating-reverse-proxy boundary that already keeps
those two probes internal; **never expose `/metrics` on the public
internet.** Series cover HTTP requests (`agnes_http_*`), the job queue and
worker runtime (`agnes_jobs_*`, `agnes_job_*`, `agnes_worker_*`),
coordination-backend health (`agnes_coordination_*`), and readiness
(`agnes_readiness`) — full series table, label reference, and the
sum-vs-max aggregation guidance for the global-queue-depth gauges live in
[`observability.md`](observability.md) → *Prometheus `/metrics`*.

The `mtier` Compose profile wires up a working example: a `prometheus`
service (`deploy/prometheus/prometheus.yml`) scrapes all four role
containers (`api1`/`api2`/`gateway`/`worker`) at `/metrics` every 15s by
Compose DNS name, plus a `cadvisor` service
(`gcr.io/cadvisor/cadvisor`) for container-level cpu/mem/network metrics.

```bash
docker compose -f docker-compose.yml -f docker-compose.postgres.yml \
  -f docker-compose.mtier.yml --profile mtier up
# Prometheus UI/API: http://localhost:9090
# cAdvisor UI:        http://localhost:8081
```

**macOS caveat:** Docker Desktop runs the daemon inside its own hidden
Linux VM, so cAdvisor's host bind mounts (`/rootfs`, `/sys`,
`/var/lib/docker`, …) resolve to that VM's filesystem rather than the Mac
host. cAdvisor still starts and gets scraped — enough to exercise the
Prometheus wiring locally — but the reported cpu/mem/disk figures are
unreliable on macOS. Treat cAdvisor data as authoritative on Linux hosts
(customer VMs) only.

A production deployment that skips the `mtier` profile should still scrape
`/metrics` on every role instance at a similar interval — nothing about
the endpoint itself is tied to Compose.

### Ops tooling (host scripts)

The `customer-instance` Terraform module's host-side scripts (deployed to
`/usr/local/bin/` and driven by systemd timers / cron) are role-split-aware:

- **`SESSION_SECRET` provisioning.** The startup script now mints and writes
  `SESSION_SECRET` through the same dedicated-Secret-Manager-secret path it
  already used for `JWT_SECRET_KEY` (fetched fresh on every boot, no on-VM
  fallback generation). Previously only `JWT_SECRET_KEY` + `AGNES_VAULT_KEY`
  were provisioned this way, so a role-split deployment through this module
  tripped the multi-process startup guard (above), which hard-fails without
  `SESSION_SECRET` set explicitly.
- **`agnes-db-backup.sh` covers the on-VM Postgres side-car.** When
  `instance.yaml::database.backend == side_car` and the container is
  actually running, the daily backup also `pg_dump`s the control-plane DB
  into the same dated backup directory and restore-canaries it (restore
  into a throwaway database, run a sanity query, drop) before declaring
  success — same 7-day retention and webhook alerting as the existing
  `system.duckdb` path. DuckDB-only deployments are unaffected.
- **`agnes-auto-upgrade.sh` does a sequential `/readyz`-gated rolling
  recreate** when it detects a role-split (m-tier) topology (dedicated
  `worker` + `gateway` services alongside 2+ named `api` replicas in the
  resolved compose config): it pulls the new image, recreates
  `worker`+`gateway` first, then walks the `api` replicas one at a time,
  waiting for each to report `/readyz` ready before touching the next. A
  hard failure of the initial `worker`+`gateway` recreate, or any single
  replica that never becomes ready within the bounded timeout, **aborts the
  whole rollout** (webhook alert, non-zero exit) without touching the
  remaining replicas, which stay on the previous image and keep serving.
  Single-container deployments are unaffected — they keep the exact
  one-shot `docker compose up -d` recreate. The script's sync-in-flight
  defer probe also now queries `GET /api/jobs?kind=data-refresh&status=running`
  (authenticated with `SCHEDULER_API_TOKEN`) alongside the existing
  `/api/sync/status` check, so it defers correctly when sync runs in a
  separate `worker` container.
- **`agnes-watchdog.sh` monitors every role container**, not just a single
  hardcoded `app` — services are enumerated via `docker compose ps`
  (`app`, `worker`, `gateway`, `api<N>`; falls back to the legacy
  `agnes-app-1` name when compose can't be resolved from the working
  directory). Every existing incident signature runs per container, naming
  it in the alert, plus a new signature: coordination-backend unreachable —
  when `coordination.backend: redis` is configured, repeated
  `CoordinationUnavailable` log hits in one container within a scan window
  fires an alert. Single-container deployments are unaffected.

**Deferred: the module doesn't opt a VM into role-split by default.** These
host scripts are role-split-*ready*, but `infra/modules/customer-instance`
does not yet ship the `mtier` Compose profile as a first-class deployable
option — an operator opts a VM in manually (set `COMPOSE_FILE`/
`COMPOSE_PROFILES` in `/opt/agnes/.env` to include
`docker-compose.mtier.yml` / `mtier`), or a later change wires the module's
own variables to do it. The load-testing / smoke harness exercises the
`mtier` Compose profile directly rather than through the module.

### Bod-3 live verification

The checks above are covered by the bash-harness unit tests (fakes for
`docker`/`curl`/`logger`/`flock`) and are static-validated (`shellcheck`,
`bash -n`) — none of it has been exercised against a real GCE VM. Before
relying on this in production, verify live:

1. **Role-split/coordination detection needs the right working directory.**
   `agnes-watchdog.sh` and `agnes-auto-upgrade.sh` resolve topology via
   `docker compose ps` / `docker compose config --services` run from the
   directory holding `docker-compose.mtier.yml` +
   `config/instance.mtier.yaml`. Without that cwd (or the files), both
   scripts silently fall back to the single-container path — role-split and
   coordination-backend detection stay inert rather than erroring loudly.
2. **A real `pg_dump` → `pg_restore` round-trip returns 0** against the
   on-VM Postgres side-car container, not just the fake-`pg_dump`/
   fake-`psql` bash-harness stand-ins.
3. **Rolling recreate against a real Caddy load balancer** leaves an aborted
   rollout serving the *old* image on the untouched replicas — confirm via
   an actual HTTP request through Caddy, not just the transcript of `docker
   compose` invocations the harness asserts on.
4. **`/readyz` via `docker compose exec` hits the newly-created replica**,
   not a stale container from a previous recreate (compose service-name
   reuse could otherwise mask a wedged container as "ready").
5. **`GET /api/jobs?kind=data-refresh&status=running` shape** matches what
   `sync_or_refresh_busy` expects against the real endpoint (not just the
   fake `curl` stub in the bash harness) — in particular the `"id"` field
   presence used as the "busy" signal.

## Cloud-chat host requirements

Agnes can serve a zero-install web chat and Slack DM bot at `/chat`. The
sandboxed runner lives in an E2B ephemeral microVM; the Agnes host only
needs RAM/CPU for the FastAPI app, ChatManager state, DuckDB, and any
open WebSockets.

**Full operator guide:** [`cloud-chat.md`](cloud-chat.md)

### Agnes server floor

Per-sandbox compute is billed by E2B (not by the Agnes host). The Agnes
server itself needs only:

- 2 GB RAM (FastAPI + ChatManager + chat_repo + WS connections)
- 1 vCPU for small teams; bump if you regularly host 50+ concurrent WS
  clients

There is no per-session RAM/CPU floor for the host any more — that
moved to the E2B template (`e2b.toml`).

### E2B account

1. Create an E2B account at https://e2b.dev and copy the API key from
   the dashboard.
2. Build the chat sandbox template: `e2b auth login` followed by
   `e2b template build` inside
   `app/initial_workspace_default/e2b-template/` (see that directory's
   README for the full walkthrough).
3. Set `E2B_API_KEY` in the Agnes server environment.
4. Put the returned template id into `chat.e2b_template_id` in
   `instance.yaml`.

Sandbox billing is visible in the operator's E2B dashboard. Agnes does
not yet surface per-session E2B cost in its own admin UI.

### Single-worker constraint

ChatManager state is in-memory. Agnes refuses to enable chat when
`UVICORN_WORKERS > 1` — ensure your Docker Compose / systemd /
Terraform unit launches a single uvicorn worker when `chat.enabled:
true`. HA support is a future spec.

## Related documentation

- [`ONBOARDING.md`](ONBOARDING.md) — end-to-end Terraform onboarding checklist
- [`CONFIGURATION.md`](CONFIGURATION.md) — `instance.yaml`, env vars, per-instance config
- [`architecture.md`](architecture.md) — internal architecture (orchestrator, extractors, DB layout)
- [`QUICKSTART.md`](QUICKSTART.md) — local development setup
- [`cloud-chat.md`](cloud-chat.md) — cloud-hosted Claude Code (`/chat` + Slack)
- [`superpowers/specs/2026-04-21-multi-customer-deployment-spec.md`](superpowers/specs/2026-04-21-multi-customer-deployment-spec.md) — design rationale for the multi-customer model
