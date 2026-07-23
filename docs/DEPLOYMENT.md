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

### DuckLake analytics backend

`analytics.backend` (`instance.yaml`) / `AGNES_ANALYTICS_BACKEND` (env) opts
an instance into `ducklake` — a DuckLake catalog replacing the rebuilt-and-
swapped `server.duckdb` file as the analytics query surface (wave-2G, WS E).
`legacy` (the current `server.duckdb` behavior) stays the zero-config
default; existing deployments are unaffected until an operator opts in.
Full architecture: [`architecture.md`](architecture.md#analytics-data-plane-legacy-vs-ducklake).

**Config:**

- `analytics.backend: ducklake` (or `AGNES_ANALYTICS_BACKEND=ducklake`).
- `ducklake.catalog_dsn` / `AGNES_DUCKLAKE_CATALOG_DSN` — a Postgres DSN
  (`postgresql://...`, the SQLAlchemy `+driver` form also accepted) for a
  shared catalog, or left unset for a DuckDB-file catalog at
  `{DATA_DIR}/analytics/catalog.ducklake` (single-process `all` mode only —
  the startup guard below refuses a file catalog on any multi-process
  topology).
- `ducklake.data_path` / `AGNES_DUCKLAKE_DATA_PATH` — where DuckLake stores
  its own data files. Defaults to `{DATA_DIR}/analytics/lake/`.
- `ducklake.snapshot_retention_days` / `AGNES_DUCKLAKE_SNAPSHOT_RETENTION_DAYS`
  — days of DuckLake snapshot history the daily maintenance job keeps before
  expiring + physically deleting the files only that snapshot referenced.
  Default 7; `0` means "no retention grace" but is floored at 1 hour
  internally (a live analyst query must never have its snapshot yanked
  out from under it mid-query — there is no hard statement timeout on
  local DuckLake queries).

**Multi-process requirement.** Any multi-process topology (role split or
`UVICORN_WORKERS > 1`) opting into `ducklake` MUST set an explicit Postgres
`ducklake.catalog_dsn` — `app/startup_guards.py::validate_deployment` refuses
to start otherwise (a DuckDB-file catalog is hard single-process: DuckDB
refuses a second same-process ATTACH of the same catalog file). The `mtier`
Compose profile (`docker-compose.mtier.yml`) demonstrates this: it flips
`analytics.backend: ducklake` with `ducklake.catalog_dsn` pointed at a
**dedicated** `agnes_ducklake` Postgres database (kept separate from the
`agnes` app-state database Alembic manages — DuckLake's `ducklake_*` catalog
tables land directly in the target database's `public` schema and would
otherwise mix with app-state tables), created via
`deploy/postgres/init-ducklake-db.sql` on first boot.

**Postgres catalog sizing.** Every DuckLake ATTACH from a Postgres-catalog
target opens exactly one libpq connection (verified directly against DuckDB
1.5.2 — no per-query connection churn). Each `api`/`gateway` replica holds
one long-lived reader connection; each `worker` process holds one writer
connection (opened lazily on first rebuild or maintenance run). Size the
catalog Postgres instance's `max_connections` for **N api/gateway replicas +
M worker processes**, plus normal headroom — not per-request, and not
per-uvicorn-worker beyond the one connection each role process itself opens.

**Maintenance job.** A daily `ducklake-maintenance` job (`app/worker/kinds.py`,
LIGHT lane; see [`jobs-classification.md`](jobs-classification.md)) runs
`merge_adjacent_files` → `ducklake_expire_snapshots` (using the retention
window above) → `ducklake_cleanup_old_files` → a catalog `VACUUM`
(Postgres-catalog only — a DuckDB-file catalog has no equivalent storage
VACUUM). Mutually exclusive with any concurrent rebuild via the same
`rebuild_mutex()` lock pair `rebuild()`/`rebuild_source()` already take, so
a long rebuild can never race a catalog-wide expire/cleanup pass. No-ops
cleanly (logs, returns) if a stray/stale job runs against a
`legacy`-configured instance.

**Migrating an existing instance.** `agnes admin analytics migrate --to
ducklake` (`POST /api/admin/analytics/migrate`) validates prerequisites —
the `ducklake` extension is loadable, and (for a Postgres catalog) a real
`ATTACH` reachability probe, auto-repairing a missing catalog database with
`CREATE DATABASE` when this instance's credentials allow it, or printing the
exact command to run manually otherwise (the expected shape when adding
DuckLake to an **existing** Postgres volume — `init-ducklake-db.sql` only
ever runs against a brand-new empty volume, so a running instance's
Postgres never gets `agnes_ducklake` created for it automatically) — then
enqueues a full rebuild into DuckLake from the on-disk extracts tree
(`analytics-migrate` job, HEAVY lane). This command never flips
`analytics.backend` itself: once the job completes (`agnes admin jobs show
<job_id>`), set `analytics.backend: ducklake` on every role process and
restart — config is read once at boot. Roll back with `agnes admin
analytics migrate --to legacy`, same shape in reverse (no prerequisites to
validate); materialized-SQL tables are not re-materialized by either
direction, they follow their own scheduler cadence.

### Signed-URL distribution (object store)

`distribution.signed_urls` / `distribution.object_store.*` (`instance.yaml`)
opt an instance into mirroring distribution parquets to an S3-compatible
bucket and having `agnes pull` download directly from it via short-TTL
presigned URLs, instead of always streaming through the app server
(wave-2H, WS F). **Off by default in effect** — with no object store
configured, nothing changes: the app-served `/api/data/{id}/download` route
(behind the reverse-proxy `file_server` bypass on multi-replica setups) stays
the only download path. Full architecture:
[`architecture.md`](architecture.md#distribution-signed-url-object-store-mirror).

**Why (L-tier only).** A single app NIC serving every analyst's `agnes pull`
is the S/M-tier reality and is fine at that scale — it's what this repo's
load-testing (see the **Live verification checklist** below) actually exercises.
At L tier (many analysts, large parquets, frequent pulls) that NIC becomes
the bottleneck; moving the bytes onto object storage — built for exactly
this fan-out — offloads it. There is no reason to turn this on below that
scale.

**`[distribution]` extra.** The S3 client (`boto3`) is an optional
dependency — install with `pip install "agnes[distribution]"` (or add
`boto3>=1.34` to your own build). Without it, `object_store()` simply
returns `None` (never imported), so a base install is completely unaffected
whether or not it configures this feature.

**Config:**

- `distribution.signed_urls: auto|on|off` (default `auto`) /
  `AGNES_DISTRIBUTION_SIGNED_URLS` (env, wins over yaml). `auto` = on when
  an object store is configured below, off otherwise. `off` is an explicit
  escape hatch that forces the app-served path even with a store
  configured.
- `distribution.object_store.bucket` / `AGNES_DISTRIBUTION_OBJECT_STORE_BUCKET`
  — required to activate the feature at all; no bucket configured means no
  object store, regardless of `signed_urls` mode.
- `distribution.object_store.endpoint_url` /
  `AGNES_DISTRIBUTION_OBJECT_STORE_ENDPOINT_URL` — leave unset for real AWS
  S3, or point at any other S3-compatible endpoint (see scope below).
- `distribution.object_store.region` / `AGNES_DISTRIBUTION_OBJECT_STORE_REGION`.
- `distribution.object_store.prefix` / `AGNES_DISTRIBUTION_OBJECT_STORE_PREFIX`
  — key prefix under the bucket (default `agnes/distribution`).
- `distribution.object_store.access_key_env` /
  `distribution.object_store.secret_key_env` — the feature never reads a
  credential value directly from `instance.yaml` or a
  `..._ACCESS_KEY`/`..._SECRET_KEY` env var. These two fields instead name
  *other* environment variables that hold the actual access key / secret
  key (same `token_env` indirection used throughout the codebase, e.g.
  `src/connection_resolver.py`) — so a credential never round-trips through
  the instance.yaml editor or a config dump.

See [`config/instance.yaml.example`](../config/instance.yaml.example) for a
complete commented `distribution:` block.

**Scope: S3-compatible only, no bundled store.** One implementation
(`src/object_store.py::S3ObjectStore`), covering AWS S3, GCS's S3-interop
endpoint, SeaweedFS (Apache-2.0, self-hostable), or any other managed
S3-compatible bucket — bring your own, this repo does not ship or run one.
MinIO is explicitly **not** recommended for this role (AGPL licensing
concerns for a source-available product bundling/recommending it). Presigned
URLs are always generated by `boto3`'s battle-tested V4 signer — never
hand-rolled signing.

**How it works.** A `distribution-mirror` job (worker role, LIGHT lane) runs
after every successful sync, uploading any downloadable local/materialized
parquet whose content changed (md5-compared against the object's stamped
metadata — idempotent, skip-if-current) to
`{prefix}/{table_id}.parquet`, then writes a small marker index recording
what's currently mirrored. `GET /api/sync/manifest` adds a `signed_url` +
`signed_url_expires_at` (15-minute TTL) to a table's entry only when that
table is in the marker index with a matching md5 — an unmirrored or stale
table simply has no `signed_url`, and the client falls back transparently.
`agnes pull` prefers `signed_url` when present, fetching straight from the
bucket; on ANY failure (network error, expired/403, md5 mismatch) it falls
back to the app-served route and md5-verifies again — the same
verify-then-atomically-promote step gates both paths, so a bad or expired
signed URL never risks corrupting a local parquet.

RBAC-wise this changes nothing: a `signed_url` only ever appears on a table
entry the caller could already download via the app-served route (the same
accessible-tables gate), and the short TTL bounds how long a leaked URL
would remain useful.

### Data apps

Hosted data apps (`data_apps.enabled`, off by default) let analysts run their
own web applications next to the data — a Flask dashboard, a Dash app, a
static SPA — using the upstream `data-app-python-js` runtime image, deployed
via internal git push (or a BYO external repo), reachable at
`/apps/<slug>/` and controlled through `agnes app`, MCP tools, or the
`/apps` web UI. Full design: [`docs/superpowers/specs/2026-07-21-data-apps-design.md`](superpowers/specs/2026-07-21-data-apps-design.md); flow summary in [`architecture.md`](architecture.md#hosted-data-apps).

**Enable it:**

1. Set `data_apps.enabled: true` in `instance.yaml` (see
   `config/instance.yaml.example` for the full block — runtime image tag,
   default resource limits, per-user app quota, idle timeout).
2. Generate the shared secret between the app and the `apps-runner` sidecar
   and put it in `.env`:

   ```bash
   APPS_RUNNER_TOKEN=$(openssl rand -hex 32)
   ```

3. Bring up the sidecar alongside the rest of the stack with the `apps`
   Compose profile:

   ```bash
   docker compose --profile apps up -d
   ```

**Subdomain routing (optional).** By default apps are reached at
`/apps/<slug>/` on the main host. Setting `data_apps.subdomain_base` (e.g.
`apps.example.com`) additionally serves each app at `<slug>.apps.example.com`
— requires a wildcard DNS record and wildcard TLS. See
[`deploy/caddy/Caddyfile.apps-subdomain`](../deploy/caddy/Caddyfile.apps-subdomain)
for the Caddy snippet to append to your reverse proxy.

**Security notes:**

- The Docker socket is mounted **only** into the `apps-runner` container
  (`services/apps_runner/api.py`) — no other process touches it. The
  sidecar's API is unpublished (internal Compose network only),
  token-gated (`X-Runner-Token`), and narrow: it only ever runs the
  configured, allowlisted runtime image with fixed mounts, never an
  arbitrary image or volume.
- Data access is **owner-inherited**: an app's REST calls run under a token
  scoped to the app owner's own grants (see spec §8). Granting someone
  access to view/open an *app* is therefore an act of publication — they see
  whatever data the app fetches under the owner's rights, even where their
  own grants are narrower (spec §10). Deactivating the owner revokes the
  app's token until an admin reassigns ownership.

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

### Live verification checklist (before production)

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

**Carried DuckLake load-test risks (wave-2G).** Two items the wave's own
review rounds flagged but did not close — verify under real load before
relying on the `ducklake` backend in production, not just under this repo's
unit/contract tests:

6. **Materialize-vs-rebuild ATTACH-race fallback-pass omission.** The legacy
   backend's filesystem-fallback pass (a materialized table's parquet landing
   on disk mid-rebuild, before its `table_registry` row is visible) was NOT
   ported to the DuckLake copy-ingest path (wave-2G Task 3 finding). Under
   load, a materialized-SQL job finishing while a `data-refresh`/
   `jira-refresh` rebuild is mid-flight can transiently drop that table from
   the lake until the *next* rebuild picks it back up — self-healing, but a
   query landing in that window sees a missing table where the legacy
   backend's fallback pass would have caught it. Verify the actual window
   size and any query-facing error surface under a realistic concurrent
   materialize+rebuild load pattern before trusting this on a large,
   frequently-materializing instance.
7. **Snapshot retention floor vs. real query durations.** The maintenance
   job's safety floor (`_MIN_RETENTION_FLOOR_SECONDS`, 1 hour — see
   `src/analytics_backend.py`) is a conservative guess at "longer than any
   real analytic query," not a measured one. Verify actual p99 query
   duration against the DuckLake reader plane under production-shaped load
   and confirm it stays comfortably under the floor — if a legitimate query
   ever runs longer, `ducklake_expire_snapshots`/`ducklake_cleanup_old_files`
   could physically delete a Parquet file a still-running query holds a
   reference to.
8. **Signed-URL distribution (wave-2H) is untested at real L-tier scale/
   load.** The full mirror -> manifest -> pull loop is covered end-to-end
   against an in-process fake object store
   (`tests/test_distribution_e2e_contract.py`), which proves the wiring but
   says nothing about presign latency, upload throughput, or bandwidth
   offload under concurrent multi-GB pulls against a *real* S3-compatible
   bucket. This repo's load-testing (item 1-5 above, on the dedicated
   `agnes-loadtest` topology) exercises the **S/M default** — no object
   store configured — so it says nothing about this feature either. Verify
   against a real bucket before relying on it to offload bandwidth at L
   tier.

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

### Multi-replica chat HA (wave-2F)

ChatManager state used to be strictly in-memory, so Agnes refused to
enable chat whenever `UVICORN_WORKERS > 1` or a role-split topology was in
play. That single-worker constraint is gone as of wave-2F: chat is allowed
on any multi-process topology once its state is coordination-backed
instead of process-local.

**Gate-lift condition.** Multi-replica/multi-worker chat requires all
three of:

- `coordination.backend: redis` (a shared, cross-process coordination
  store — see [Coordination backend](#coordination-backend) above);
- Postgres app-state (`DATABASE_URL` / `database.backend`), already
  required by `app/startup_guards.py::validate_deployment` for any
  multi-process topology; and
- explicit `JWT_SECRET_KEY` / `SESSION_SECRET` (same guard).

Under the default `coordination.backend: memory`, `chat.enabled: true`
with `UVICORN_WORKERS > 1` or a role split is still refused outright —
process-local ChatManager state can't be shared across workers, so the
refusal is unchanged there. Once `redis` is configured, the rest of the
multi-process contract above is already satisfied by construction, and
`app/main.py` starts a real `ChatManager` on every `Role.GATEWAY` process.

**What makes this safe.** Every piece of ChatManager state that used to be
a process-local Python dict now rides the coordination backend:

- **Routing leases** (`app/chat/routing.py`) — one `chat:{chat_id}` lease
  names which gateway replica currently owns a session's live sandbox/
  runner. Claimed when a session goes live, renewed on the existing
  ~60s idle-reaper heartbeat, released on teardown.
- **Frame envelope + replay** (`app/chat/frame_seq.py`,
  `app/chat/replay.py`) — every outbound frame carries a monotonic `seq`
  and is appended to a bounded `chat-out:{chat_id}` stream (`MAXLEN`
  1000). A WS reconnect sends the highest `seq` it last saw as
  `?last_seq=`; the gateway either replays the gap in order or, if the
  gap can't be confidently closed (stream reset, or the watermark evicted
  past `MAXLEN`), sends a `full_refresh` control frame and the client
  reloads persisted history instead of risking a silent gap.
- **Inbound command routing** (`app/chat/inbound.py`) — a message or
  command (`/agnes` slash command, Slack event, `kill`/`cancel`) that
  lands on a gateway which does **not** own the session's routing lease is
  published to a `chat-in:{chat_id}` coordination stream instead of
  spawning a second runner; the owning gateway's inbound-consumer task
  delivers it to the local runner in order.
- **Claim-then-respawn takeover** (`app/chat/manager.py`,
  `ChatManager.attach` / `_takeover_foreign_session`) — a WS reconnect
  landing on a gateway that doesn't own the lease steals it, destroys the
  old sandbox, spawns a fresh runner, and replays the last few user turns
  for continuity.
- **Desktop/browser notifications** absorbed into the same coordination
  fabric (per-user pub/sub channel `notify:{user}`) — see
  [`architecture.md`](architecture.md)'s Coordination Backend section.

**v1 trade-off — in-flight turn is lost on takeover.** Claim-then-respawn
is *not* a live handoff: it destroys the old sandbox and starts a fresh
one. Any turn that was in flight on the old gateway at the moment of
takeover is lost — the client sees the new runner start clean, same as a
plain process restart already implies. This is an accepted v1 limitation,
not a bug; a live cross-process handoff would need a persisted
relay-protocol-version column and is deferred (tracked as a follow-up, not
scheduled).

**Operator guidance: replicas over workers.** Prefer **N × single-worker
gateway replicas** behind a load balancer over a single gateway process
with `UVICORN_WORKERS > 1`. Each uvicorn worker is a separate OS process
with its own `ChatManager` and its own routing-lease identity
(`app.chat.routing.this_gateway_id()` is `<hostname>:<pid>` — every worker
in the same container shares the hostname but has a distinct pid, so they
still count as distinct gateways to the routing-lease mechanism). A load
balancer that round-robins across an in-process multi-worker gateway
picks a *different worker* on every reconnect with no way to prefer the
one that already owns the lease — every reconnect becomes a takeover,
needlessly losing in-flight turns. Running N replicas, each a single
worker, keeps the same total capacity but makes "the gateway that owns
this session" a property the load balancer *can* act on (see below), where
per-worker round-robin inside one process cannot.

**Load-balancer routing rule (required for role-split topologies).** Only
`Role.GATEWAY` processes construct a `ChatManager`; on api-role replicas
`app.state.chat_manager` is `None`. The load balancer must therefore route
the chat surfaces to gateway-role upstreams:

- `/api/chat/*` — the chat WebSockets (stream/join) and the session
  lifecycle REST endpoints;
- `/api/notifications/ws` — the desktop/browser notifications WS (absorbed
  into the gateway role in wave-2F); and
- `/api/slack/*` — **when Slack runs in webhook mode** (Socket Mode opens
  an outbound connection from the gateway leader and needs no inbound
  route).

The reference `deploy/caddy/Caddyfile.mtier` implements this rule: a
path-matcher-scoped `reverse_proxy` to the `gateway` upstream placed
*before* the `api1`/`api2` catch-all (Caddy evaluates same-name directives
in order of appearance). Slack webhooks that land on an api replica anyway
do not crash: the handlers detect the missing `ChatManager` and degrade to
a **thin-producer** path — resolve/create the session row, enforce the
same sender limits, persist the user message, and publish it to the
`chat-in:{chat_id}` stream with its Slack origin so the owning gateway
re-establishes the reply sink (kills/cancels forward as `control`
entries the same way). That fallback can only *hand off*, though — a
session no gateway ever attaches has no inbound consumer to deliver to —
so treat the gateway routing rule as the reference topology and the
producer path as graceful degradation, not as an alternative.

**WebSocket affinity is recommended, not required.** Session-routing
leases plus claim-then-respawn takeover mean the chat feature is
*correct* with zero LB stickiness — any replica can serve any session,
taking over the lease as needed. But every takeover pays the v1 cost
above (fresh sandbox, lost in-flight turn), so **configuring WS/session
affinity at the load balancer** (e.g. cookie- or IP-hash-based sticky
sessions, so a client's reconnect tends to land back on the gateway that
already owns its session) meaningfully reduces how often that cost is
paid. Put plainly: **no stickiness is REQUIRED for correctness — affinity
is RECOMMENDED for UX** (fewer needless takeovers, fewer dropped in-flight
turns on an ordinary reconnect).

### Chat LLM credential failures (runbook)

When the LLM API key the chat broker uses is invalid, expired, or the account
is unfunded, chat is effectively down: the in-sandbox agent returns a synthetic
error message and the failure is otherwise opaque. Agnes classifies the failure
and surfaces it to admins so the cause is unambiguous.

**Where the signal shows up:**

- **Admin UI** — the *Cloud chat readiness* panel in **Admin → Server config**
  shows a red banner naming the exact fault:
  - *LLM key rejected* — the key is invalid, expired, or lacks permission (HTTP 401/403).
  - *LLM account unfunded* — the key is valid but the account has no credit
    ("credit balance too low", HTTP 400).
  - *LLM provider error* — network / rate-limit / provider outage.
- **API** — `GET /admin/chat/readiness` returns an `llm_runtime` object
  (`{reason, detail, status_code, at}`), or `null` when the last forward succeeded.
- **Audit log** — each failure records a `broker_llm_auth_failure` row
  (reason + status only, never the key value).

**Remediation:**

1. In **Admin → Server config → Cloud chat**, set/rotate the Anthropic key via
   the *configure secrets* field and save. This persists to the env-overlay and
   survives restarts.
2. Click **Test connection** to confirm the new key authenticates (and that the
   account is funded — an *unfunded* fault means the key is fine but the LLM
   provider account needs credit added on the provider's side).
3. The `llm_runtime` signal clears automatically on the next successful chat
   forward.

Keyless (workload-identity) auth for the LLM is out of scope here (tracked
separately); in that mode use **Test connection**, which probes the federated
token path instead of a static key.

## Related documentation

- [`ONBOARDING.md`](ONBOARDING.md) — end-to-end Terraform onboarding checklist
- [`CONFIGURATION.md`](CONFIGURATION.md) — `instance.yaml`, env vars, per-instance config
- [`architecture.md`](architecture.md) — internal architecture (orchestrator, extractors, DB layout)
- [`QUICKSTART.md`](QUICKSTART.md) — local development setup
- [`cloud-chat.md`](cloud-chat.md) — cloud-hosted Claude Code (`/chat` + Slack)
- [`superpowers/specs/2026-04-21-multi-customer-deployment-spec.md`](superpowers/specs/2026-04-21-multi-customer-deployment-spec.md) — design rationale for the multi-customer model
