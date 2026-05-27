# Postgres cut-over runbook

Operator playbook for the release that introduces the Postgres side-car as
the default customer-instance deploy shape. Read this before bumping your
`infra-vX.Y.Z` module pin past the cut-over release.

This document is vendor-agnostic — substitute `<customer>` and `<your-host>`
with your own values. Commands assume you SSH into the customer-instance VM
and that the app lives at `/opt/agnes` (the default install path).

---

## What changed

- **Postgres is now a side-car container on the customer-instance VM.** The
  upstream `customer-instance` Terraform module provisions a per-instance
  password in Secret Manager (`agnes-<customer>-postgres-password`) and the
  VM startup-script writes `POSTGRES_PASSWORD`, `DATABASE_URL` and
  `COMPOSE_FILE` into `/opt/agnes/.env` before `docker compose up`.
- **Persistence lives on the existing data disk.** The `postgres_data` named
  volume is bound to `/data/postgres`, which is already covered by your daily
  snapshot policy. The startup-script `chown`s the directory to uid `70`
  (the `postgres:16-alpine` container user) before the side-car boots.
- **App-state writes route to Postgres** via the factory in
  `src/repositories/__init__.py` when `DATABASE_URL` is set. The DuckDB
  analytics layer — `analytics.duckdb`, parquet views, the BigQuery extension
  — is **unchanged**. Only the system tables (users, audit_log, RBAC,
  table_registry, sync_state, recipes, memory domains, …) move to PG.
- **Two one-shot services run on every `docker compose up`.** `migrate`
  runs `alembic upgrade head` against the new database; `data-migrate` runs
  `python -m scripts.migrate_duckdb_to_pg` to copy any remaining rows from
  the DuckDB `system.duckdb` snapshot into PG. Both are idempotent — first
  deploy backfills, subsequent deploys are no-ops. `app` and `scheduler`
  both `depends_on: { data-migrate: { condition: service_completed_successfully } }`
  so no request hits a partially-migrated database.

---

## Standard deploy

1. **Bump the module pin** in your customer infra repo:

   ```hcl
   module "agnes" {
     source = "git::https://github.com/keboola/agnes-the-ai-analyst.git//infra/modules/customer-instance?ref=infra-vX.Y.Z"
     # ...
   }
   ```

2. **`terraform apply`.** This creates the new Secret Manager triple
   (`google_secret_manager_secret` + `_secret_version` + IAM binding for the
   VM service account) and grants `roles/secretmanager.secretAccessor` to the
   VM. No VM re-create is required — the secret is read at boot by the
   startup-script.

3. **Roll the VM forward.** SSH in and run the auto-upgrade script (or wait
   for the next cron tick — default cadence is 5 min):

   ```bash
   sudo /usr/local/bin/agnes-auto-upgrade.sh
   ```

   This re-runs the startup-script flow: pulls the new password from Secret
   Manager, ensures `/data/postgres` exists with uid `70` ownership, writes
   the three env vars into `/opt/agnes/.env`, and runs
   `docker compose pull && docker compose up -d`.

4. **Verify the app is healthy:**

   ```bash
   curl -fsS http://localhost:8000/api/health
   ```

   Expect HTTP 200 and a JSON body.

5. **Confirm new writes land in PG:**

   ```bash
   cd /opt/agnes
   docker compose exec postgres psql -U agnes -d agnes -c "SELECT count(*) FROM audit_log;"
   ```

   Drive a write (e.g. log in via the UI) and re-run the count — it should
   increment.

---

## Verify after deploy

Spot checks to run once the auto-upgrade finishes:

**All services healthy and migrate/data-migrate exited 0:**

```bash
cd /opt/agnes
docker compose ps
```

Expect `app`, `scheduler`, `postgres` in `running` (healthy where a healthcheck
is defined); `migrate` and `data-migrate` in `exited (0)`.

**Schema head matches the latest revision:**

```bash
docker compose exec postgres psql -U agnes -d agnes \
  -c "SELECT version_num FROM alembic_version;"
```

Compare against the latest revision in `alembic/versions/` shipped with the
release (the highest `NNNN_` prefix).

**Row-count sanity for one migrated table** — compare PG to the DuckDB
snapshot that lives on disk untouched by this cut-over:

```bash
docker compose exec postgres psql -U agnes -d agnes \
  -c "SELECT count(*) FROM users;"

docker compose exec app python -c "
import duckdb
c = duckdb.connect('/data/state/system.duckdb', read_only=True)
print(c.execute('SELECT count(*) FROM users').fetchone())
"
```

The two counts should match. Repeat for `audit_log`, `table_registry`,
`sync_state`, and any other table the data-migrate script copies (see
`scripts/migrate_duckdb_to_pg.py` for the full list).

---

## Operator override — managed Postgres

If you don't want the side-car container — for example to share one Cloud
SQL / RDS / Supabase / Neon instance across several Agnes VMs, or to keep
backups in your existing managed-DB tier — point the app at a managed PG and
disable the overlay:

1. **SSH into the VM** and edit `/opt/agnes/.env`:

   ```bash
   sudo -e /opt/agnes/.env
   ```

   - Replace the side-car URL
     `DATABASE_URL=postgresql+psycopg://agnes:...@postgres:5432/agnes`
     with your managed-PG URL, e.g.
     `DATABASE_URL=postgresql+psycopg://agnes:...@<your-managed-host>:5432/agnes`.
   - Remove the line
     `COMPOSE_FILE=docker-compose.yml:docker-compose.postgres.yml:docker-compose.host-mount.yml`
     (or shorten it to the baseline
     `COMPOSE_FILE=docker-compose.yml:docker-compose.host-mount.yml`) so the
     postgres overlay is not included.

2. **Restart the stack:**

   ```bash
   sudo systemctl restart agnes
   # or, equivalently:
   cd /opt/agnes && docker compose up -d
   ```

3. **Run `alembic upgrade head` against the managed PG** as part of your
   deploy pipeline (one-shot, no long-running service required):

   ```bash
   docker run --rm \
     -e DATABASE_URL="postgresql+psycopg://agnes:...@<your-managed-host>:5432/agnes" \
     ghcr.io/keboola/agnes-the-ai-analyst:stable \
     alembic upgrade head
   ```

   If this is the first cut-over from DuckDB, also run the data-migrate
   step once against the managed URL (it's idempotent — safe to re-run):

   ```bash
   docker run --rm \
     -v /data/state:/data/state:ro \
     -e DATABASE_URL="postgresql+psycopg://agnes:...@<your-managed-host>:5432/agnes" \
     ghcr.io/keboola/agnes-the-ai-analyst:stable \
     python -m scripts.migrate_duckdb_to_pg
   ```

   The Terraform module still provisions the side-car password secret in
   this layout — it's unused but harmless. If you want to drop it, override
   the `enable_postgres_secret` variable (or whatever your module pin
   exposes) in your TF root.

---

## Rollback to DuckDB

The DuckDB system database at `/data/state/system.duckdb` is **never deleted
by this stack**. The cut-over copies rows into PG; it does not remove the
source file. Rolling back is therefore just turning the overlay off again:

1. **SSH in, edit `/opt/agnes/.env`:**

   ```bash
   sudo -e /opt/agnes/.env
   ```

   - Remove the `DATABASE_URL=...` line.
   - Remove or shorten `COMPOSE_FILE` to omit the postgres overlay.

2. **Roll the VM forward:**

   ```bash
   sudo /usr/local/bin/agnes-auto-upgrade.sh
   # or, equivalently:
   cd /opt/agnes && docker compose up -d
   ```

3. **Verify:**

   ```bash
   curl -fsS http://localhost:8000/api/health
   ```

   Drive a write (log in, register a table) and confirm it lands in DuckDB:

   ```bash
   docker compose exec app python -c "
   import duckdb
   c = duckdb.connect('/data/state/system.duckdb', read_only=True)
   print(c.execute('SELECT count(*) FROM audit_log').fetchone())
   "
   ```

The PG side-car keeps its data on `/data/postgres` for any future
re-cutover; **nothing is destroyed by rollback**. If you want to reclaim
the disk space, `sudo rm -rf /data/postgres/*` after confirming the
rollback is healthy.

---

## `POSTGRES_PASSWORD` rotation

Rotate the side-car password when a credential leaks or on a scheduled
cadence. The Secret Manager triple is the source of truth; the VM
startup-script reads from it on every boot but does **not** re-read mid-life,
so an `.env` edit + `docker compose restart` is required after writing a new
secret version:

```bash
# 1. Generate a new password (URL-safe — no /, +, =).
NEW=$(openssl rand -base64 32 | tr -d /=+ | head -c 32)

# 2. Add a new version to the Secret Manager secret.
echo -n "$NEW" | gcloud secrets versions add \
  agnes-<customer>-postgres-password --data-file=-

# 3. On the VM, edit /opt/agnes/.env and replace POSTGRES_PASSWORD
#    (and the password embedded in DATABASE_URL) with the new value.
sudo -e /opt/agnes/.env

# 4. Restart only the affected services.
cd /opt/agnes
docker compose restart postgres app scheduler
```

After step 4, also re-run the password update inside PG itself if the
existing role's password needs to change at the database level (the
side-car only sets `POSTGRES_PASSWORD` at first-init):

```bash
docker compose exec postgres psql -U agnes -d agnes \
  -c "ALTER ROLE agnes WITH PASSWORD '<new-password>';"
```

Then disable any older secret versions in Secret Manager once the new
password is confirmed in use.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `migrate` exits non-zero, `app` refuses to start | Failed `alembic upgrade head` — constraint violation, missing extension, or a data-shape conflict against an existing PG | `cd /opt/agnes && docker compose logs migrate`; address the offending revision (often a hand-applied schema change on a managed PG); re-run `docker compose up -d`. |
| `data-migrate` hangs > 5 min | Very large `audit_log`, a stuck PG lock, or DuckDB file held open by another process | `docker compose logs -f data-migrate`; if a single table is the culprit, run the script manually with `--only <table>` to triage: `docker compose run --rm data-migrate python -m scripts.migrate_duckdb_to_pg --only audit_log`. |
| `/data/postgres` permission denied at container boot | Startup-script didn't run, or ran without the `chown -R 70:70 /data/postgres` step (older infra pin) | SSH in: `sudo chown -R 70:70 /data/postgres && cd /opt/agnes && docker compose restart postgres`. |
| Disk full on `/data` | PG data + DuckDB snapshot + parquet uploads exceed the data-disk size | Resize the `data` disk in your customer infra TF (the `data_disk_size_gb` variable on the `customer-instance` module); `terraform apply`; reboot the VM so the kernel picks up the larger device, then `sudo resize2fs /dev/disk/by-id/google-data-1` (or your distro's equivalent) to grow the filesystem. |
| `psql: FATAL: password authentication failed for user "agnes"` after a rotation | `/opt/agnes/.env` updated but the PG role's password wasn't changed inside the database | `docker compose exec postgres psql -U agnes -d agnes -c "ALTER ROLE agnes WITH PASSWORD '<new>';"` — see the rotation section above. |
| App logs show `sqlalchemy.exc.OperationalError: connection refused` | `postgres` service is unhealthy or still starting | `docker compose ps` — if postgres is restarting, `docker compose logs postgres`; if it's healthy but the app can't reach it, confirm `DATABASE_URL` host is `postgres` (the compose service name), not `localhost`. |
| `COMPOSE_FILE` env not honored — overlay services missing | Older `agnes-auto-upgrade.sh` (pre-Task 2B.1) that didn't `export COMPOSE_FILE` from `/opt/agnes/.env` | Pull the latest `agnes-auto-upgrade.sh` from the upstream `scripts/ops/` directory and replace `/usr/local/bin/agnes-auto-upgrade.sh`; or invoke `docker compose -f docker-compose.yml -f docker-compose.postgres.yml -f docker-compose.host-mount.yml up -d` explicitly once to recover. |

---

## Related docs

- [`docs/migrations.md`](migrations.md) — Alembic conventions, repository
  cross-engine contract, the DuckDB → PG data-migrate script.
- [`docs/DEPLOYMENT.md`](DEPLOYMENT.md) — full deploy guide (compose
  topology, TLS, image channels).
- [`CHANGELOG.md`](../CHANGELOG.md) — release notes; look for the entry that
  bumps the `customer-instance` module to the cut-over version.
