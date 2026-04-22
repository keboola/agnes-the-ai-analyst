# Manual deploy helper — Agnes on an existing VM (GRPN pattern)

A `make`-based helper for deploying and operating Agnes on an **existing** GCE VM when the full Terraform flow is blocked — typically by organization policies that forbid SA JSON key creation or by missing IAM delegation. This is the pattern we used on GRPN's `foundryai-development` during the 2026-04-22 hackathon.

It is **not** a replacement for the full Terraform module — only a stopgap while the proper flow is being unblocked. See [Migration path](#migration-path) below.

## When to use this

Use this helper when **all** are true:

- A target VM already exists in the customer's GCP project (we don't create it)
- You (or the deploy SA) do **not** have `roles/resourcemanager.projectIamAdmin` on that project, **or** the org has `constraints/iam.disableServiceAccountKeyCreation` enabled
- The customer is OK with a single-VM, single-node Agnes (no prod + dev split for now)
- Data persistence on the VM's boot disk is acceptable (no persistent disk attached → data loss on VM recreate)

Any of those false → go the Terraform route via [`docs/HACKATHON.md`](../../docs/HACKATHON.md) Part 1.

## What it does (and doesn't)

| Aspect | Manual helper (this) | Full Terraform flow |
|---|---|---|
| VM provisioning | Reuses existing VM | Creates a dedicated `agnes-prod` + optional `agnes-dev` VMs |
| Docker install | Inline `curl get.docker.com \| sh` on first deploy | Part of the module's startup script |
| Secrets | Plain `.env` on VM (`chmod 600`) | GCP Secret Manager, read by VM SA |
| Service account | Uses the VM's existing SA, whatever that is | Dedicated `agnes-<customer>-vm` with scoped `secretmanager.secretAccessor` only |
| Data persistence | Boot disk, ephemeral across VM recreate | Separate persistent disk (`/data` bind-mount), daily snapshot + 30-day retention |
| Auto-upgrade | `install-cron` target deploys the same cron script the module uses | Built into the startup script |
| Monitoring / alerts | None | Uptime check + alert policy per VM |
| Backup | None | Daily snapshot schedule |
| Branch-aware dev VMs | Not supported (single VM) | `dev_instances` list — one VM per branch/engineer |
| CI/CD | None — manual `make deploy` | GitHub Actions: PR → plan → apply (dev auto, prod gated) |

The helper covers the **runtime** aspects (pull image, restart, logs, access) but skips the infra-as-code posture.

## One-time setup

Done for GRPN during the 2026-04-22 hackathon. Re-useable template for any future customer in a similar constrained environment:

### 1. Verify access to the VM

```bash
gcloud compute ssh $VM --zone=$ZONE --project=$PROJECT --command='whoami'
```

If this works, you have SSH via OS Login or your own key. IAP tunnel auto-kicks in if the VM has no external IP. No further auth setup is needed.

### 2. Install Docker + compose plugin

```bash
gcloud compute ssh $VM --zone=$ZONE --project=$PROJECT --command="
  curl -fsSL https://get.docker.com | sudo sh
  sudo apt-get install -y -qq docker-compose-plugin
"
```

### 3. Prepare app directory and data root

```bash
gcloud compute ssh $VM --zone=$ZONE --project=$PROJECT --command="
  sudo mkdir -p /opt/agnes /data/state /data/analytics /data/extracts
  sudo chown -R \$USER:\$USER /opt/agnes
  cd /opt/agnes
  curl -fsSL https://raw.githubusercontent.com/keboola/agnes-the-ai-analyst/main/docker-compose.yml -o docker-compose.yml
  curl -fsSL https://raw.githubusercontent.com/keboola/agnes-the-ai-analyst/main/docker-compose.prod.yml -o docker-compose.prod.yml
  curl -fsSL https://raw.githubusercontent.com/keboola/agnes-the-ai-analyst/main/docker-compose.host-mount.yml -o docker-compose.host-mount.yml
"
```

### 4. Write `.env` (plain, chmod 600)

```bash
JWT=$(openssl rand -hex 32)
cat > /tmp/agnes-env <<EOF
JWT_SECRET_KEY=$JWT
DATA_DIR=/data
DATA_SOURCE=csv          # or bigquery / keboola
SEED_ADMIN_EMAIL=<your@email>
LOG_LEVEL=info
AGNES_TAG=stable
EOF
gcloud compute scp /tmp/agnes-env $VM:/tmp/.env --zone=$ZONE --project=$PROJECT
gcloud compute ssh $VM --zone=$ZONE --project=$PROJECT --command="
  sudo install -m 600 -o \$USER -g \$USER /tmp/.env /opt/agnes/.env
  rm /tmp/.env
"
rm /tmp/agnes-env
```

If `DATA_SOURCE=keboola`, add `KEBOOLA_STORAGE_TOKEN=...` + `KEBOOLA_STACK_URL=...` lines. Same for any BQ / custom data source credentials — they all live in this one `.env`.

### 5. First boot

```bash
make deploy
make bootstrap-admin PASSWORD=<strong-initial>
```

`deploy` pulls the image + starts containers. `bootstrap-admin` hits `/auth/bootstrap` to activate the seed admin.

### 6. (Optional) Auto-upgrade

```bash
make install-cron
```

Installs the same 5-minute polling cron used by the Terraform module. After this, every new `:stable` image digest is picked up within ~5 min without any human action.

## Everyday operations

From the repo root (tested defaults target GRPN's `foundryai-development`):

```bash
make -C scripts/grpn help           # list all targets
make -C scripts/grpn status         # is it up?
make -C scripts/grpn version        # what's deployed right now
make -C scripts/grpn logs           # tail app logs
make -C scripts/grpn deploy         # pull :stable + recreate
make -C scripts/grpn tunnel         # IAP tunnel → http://localhost:8000
```

## Configuration

All targets read overridable variables at the top of `Makefile`. Defaults target GRPN's `foundryai-development`. For other VMs/projects:

```bash
# one-off override
make -C scripts/grpn status \
    PROJECT=other-project \
    ZONE=us-central1-a \
    VM=other-vm

# or fork this Makefile into `scripts/<customer>/Makefile` with different defaults
```

| Variable | Default | Purpose |
|---|---|---|
| `PROJECT` | `prj-grp-foundryai-dev-7c37` | GCP project ID |
| `ZONE` | `us-central1-a` | VM zone |
| `VM` | `foundryai-development` | Instance name |
| `APP_DIR` | `/opt/agnes` | Where compose files + `.env` live on the VM |
| `LOCAL_PORT` | `8000` | Local port for `tunnel` target |
| `VM_PORT` | `8000` | Port the app listens on inside the VM |
| `IMAGE` | `ghcr.io/keboola/agnes-the-ai-analyst` | GHCR image repo |
| `ADMIN_EMAIL` | `e_zsrotyr@groupon.com` | Default bootstrap email |

## Files

```
scripts/grpn/
├── Makefile                 # the helper itself
├── agnes-auto-upgrade.sh    # deployed by `make install-cron` to /usr/local/bin/
└── README.md                # this file
```

Plus the deploy log: [`docs/superpowers/plans/2026-04-22-grpn-deploy-learnings.md`](../../docs/superpowers/plans/2026-04-22-grpn-deploy-learnings.md) — lists all the org-policy constraints encountered and their workarounds.

## Migration path

Once the blockers are lifted, move to the proper Terraform flow:

1. **Get `roles/resourcemanager.projectIamAdmin`** on the customer project (ask the GRPN admin to grant it).
2. **Create a WIF pool + provider** in the customer project (doesn't require SA JSON keys; bypasses `iam.disableServiceAccountKeyCreation`). Draft patch pending on [`bootstrap-gcp.sh`](../bootstrap-gcp.sh) — track via GitHub issue tagged `wif`.
3. **Migrate**: run the new `bootstrap-gcp.sh --wif`, create a private infra repo from [`keboola/agnes-infra-template`](https://github.com/keboola/agnes-infra-template), `terraform apply` → this creates a **new** Agnes VM alongside the existing `foundryai-development`.
4. **Optional** — move data from the manual VM to the TF VM with a `tar` snapshot through GCS (see the original migration in [`docs/superpowers/plans/2026-04-21-deployment-log.md`](../../docs/superpowers/plans/2026-04-21-deployment-log.md) "Data migration" section).
5. **Decommission** the manual deploy: `make stop` + delete `/opt/agnes/` on the VM.

## Caveats

- **Single VM, single point of failure.** No dev/prod split.
- **No automatic backups.** If someone deletes the VM, data is gone (30-day boot-disk retention from GCP default only).
- **Plain-text secrets in `.env`.** Acceptable for IAP-only internal VM; **not** acceptable if the VM ever gets an external IP.
- **No drift detection.** Anyone with SSH can hand-edit `.env` or compose files without leaving an audit trail. The Terraform flow's `ignore_changes` + `-replace` pattern is the correct version of this.

## See also

- [`docs/HACKATHON.md`](../../docs/HACKATHON.md) — the full TL;DR for deploy and develop (the TF path)
- [`docs/ONBOARDING.md`](../../docs/ONBOARDING.md) — detailed per-customer Terraform onboarding
- [`docs/DEPLOYMENT.md`](../../docs/DEPLOYMENT.md) — comparison of TF vs docker-compose deployment strategies
- [`infra/modules/customer-instance/`](../../infra/modules/customer-instance/) — the Terraform module this helper shadows
