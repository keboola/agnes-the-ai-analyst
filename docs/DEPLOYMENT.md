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

`scripts/grpn/agnes-tls-rotate.sh` is the single entry point — it handles fetch, self-signed fallback, auto-generation on missing key, atomic cert swap, and Caddy reload. Env vars it reads:

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
