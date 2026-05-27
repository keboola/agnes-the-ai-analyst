# Postgres migrations playbook

Agnes's app state lives in Postgres (via SQLAlchemy 2.0 + Alembic). Analytics
stays on DuckDB. This document is the operator + developer playbook for the
PG side; the DuckDB inline ladder in `src/db.py` is unrelated and is being
retired as repository ports complete.

## Module layout

```
alembic.ini                              ; Alembic config, no DB URL
migrations/                              ; revision chain
  env.py                                 ; reads AGNES_DB_URL / DATABASE_URL
  script.py.mako
  versions/
    0001_baseline.py                     ; empty anchor
    0002_audit_log.py
    0003_rbac.py                         ; users + user_groups + members + grants
    0004_ops_triad.py                    ; table_registry + sync_state + sync_history
    0005_config_and_pat.py               ; metric_definitions + instance_templates + PATs
    0006_lookup.py                       ; view_ownership + column_metadata + bq_cache + sync_settings

src/db_pg.py                             ; engine + session singleton
src/models/                              ; SQLAlchemy 2.0 models (one file per cluster)
src/repositories/*_pg.py                 ; Postgres impls (mirror DuckDB ones)

tests/db_pg/                             ; PG fixtures + tests
  conftest.py                            ; pg_engine via testcontainers/embedded/pgserver
  snapshot.py                            ; schema snapshot for round-trip tests
  test_alembic_skeleton.py
  test_alembic_roundtrip.py              ; round-trip + drift discipline
  test_audit_contract.py                 ; cross-engine contract test pattern
  test_*_pg.py                           ; per-cluster PG-side tests

scripts/migrate_duckdb_to_pg/__init__.py ; data migration framework
scripts/migrate_duckdb_to_pg/__main__.py ; CLI: python -m scripts.migrate_duckdb_to_pg
```

## URL resolution

```bash
# Production
export AGNES_DB_URL="postgresql+psycopg://agnes:***@10.x.y.z:5432/agnes"

# Dev (defaults to a local pgserver-bundled binary; no install needed)
unset AGNES_DB_URL DATABASE_URL  # tests pick up automatically
```

Resolution order: explicit `cfg.attributes["sqlalchemy.url"]` (used by tests) →
`AGNES_DB_URL` env → `DATABASE_URL` env. No silent default to SQLite or
file paths.

## Local dev via Docker Compose

The repo ships `docker-compose.postgres.yml` as a backward-compatible
**overlay**: existing `docker compose up` continues to run the
DuckDB-only path; adding the overlay brings up a Postgres service plus
a one-shot migrate service.

```bash
# Bring up app + Postgres, run migrations, then start uvicorn
docker compose -f docker-compose.yml -f docker-compose.postgres.yml up

# Or set once in your shell so plain `docker compose up` picks it up
export COMPOSE_FILE=docker-compose.yml:docker-compose.postgres.yml
docker compose up
```

What the overlay does:

- Adds a `postgres:16-alpine` service with a named volume (`postgres_data`).
- Adds a one-shot `migrate` service that runs `alembic upgrade head`
  against the Postgres above, then exits.
- Wires `AGNES_DB_URL=postgresql+psycopg://agnes:${POSTGRES_PASSWORD}@postgres:5432/agnes`
  into both `migrate` and `app`. The `app` service waits for `migrate`
  to exit 0 before booting uvicorn, so a botched upgrade blocks the
  whole stack with a clear log trail.
- Binds Postgres to `127.0.0.1:5432` on the host so a misconfigured
  firewall can't expose it to the public internet.

`POSTGRES_PASSWORD` is read from `.env`; defaults to `agnes` for
zero-config local dev (change before any multi-user / exposed
deployment).

## Production wiring

For managed Postgres (Cloud SQL, RDS, Azure DB), **do NOT use the
postgres overlay** — point `AGNES_DB_URL` at the managed instance and
run the migration step in your deploy pipeline.

### Cloud SQL — TCP from private IP

```bash
# In .env or Secret Manager → injected into the systemd unit's
# EnvironmentFile= (or container env)
AGNES_DB_URL=postgresql+psycopg://agnes:${PG_PW}@10.x.y.z:5432/agnes
```

### Cloud SQL — Unix socket (Cloud SQL Auth Proxy or private IP)

```bash
# Note the URL-encoded socket path: %2F = /
AGNES_DB_URL=postgresql+psycopg://agnes:${PG_PW}@/agnes?host=/cloudsql/${PROJ}:${REGION}:${INST}
```

The `cfg.attributes["sqlalchemy.url"]` trick in `migrations/env.py`
exists specifically so the `%`-encoded socket path doesn't break
configparser's interpolation.

### Migration step in CI/CD

Run `alembic upgrade head` against the production DB **before** the new
image starts serving traffic — this is the standard expand-then-deploy
pattern. The migration is idempotent (re-running on a head DB is a
no-op), so a re-deploy after a failed image pull is safe.

```bash
# Recommended pattern: a one-shot pre-deploy job in your pipeline
docker run --rm \
  -e AGNES_DB_URL="${AGNES_DB_URL}" \
  ghcr.io/keboola/agnes-the-ai-analyst:${IMAGE_TAG} \
  alembic upgrade head

# Then deploy the app image. The image already contains alembic + the
# migrations dir — no extra build step needed.
```

For rollback discipline: every Alembic revision in this repo has a
real `downgrade()` body, validated by `test_full_chain_roundtrip` and
`test_pairwise_roundtrip` on every PR. To roll back one revision in
production:

```bash
docker run --rm -e AGNES_DB_URL=... ghcr.io/keboola/agnes-the-ai-analyst:${IMAGE_TAG} \
  alembic downgrade -1
```

### Connection-pool tuning

`src/db_pg.py` defaults to `pool_size=5, max_overflow=10` — i.e. up to
15 concurrent connections per app process. Cloud SQL's per-instance
connection cap (default 100, configurable up to thousands depending on
tier) is the binding constraint. For a 3-VM MIG running 1 uvicorn
worker each, 3 × 15 = 45 connections — comfortably inside the default
cap. Scale `pool_size` proportionally if you increase uvicorn workers
per VM.

## Running migrations

```bash
# Upgrade to head
AGNES_DB_URL=... alembic upgrade head

# Downgrade by one revision
AGNES_DB_URL=... alembic downgrade -1

# Generate offline SQL (for DBA review)
AGNES_DB_URL=... alembic upgrade head --sql > /tmp/up.sql

# Autogenerate a fresh revision from a model change
AGNES_DB_URL=... alembic revision --autogenerate -m "your message"
# (then hand-review the file in migrations/versions/)
```

## Adding a new model

The pattern this repo has standardised on:

1. **Define the SQLAlchemy model** under `src/models/<cluster>.py`. Import
   `Base` from `src.db_pg`.
2. **Add the import** to `src/models/__init__.py` so Alembic's
   autogenerate sees the new metadata.
3. **Confirm the drift test fires red** —
   `pytest tests/db_pg/test_alembic_roundtrip.py::test_no_model_migration_drift`
   should fail with "model vs migration drift detected" listing your new
   table.
4. **Generate the migration** —
   `alembic revision --autogenerate -m "<table_name>"`. Read the
   generated file; verify the `downgrade()` body is the true inverse of
   `upgrade()` (it should explicitly `drop_index` every index added in
   `upgrade()` before `drop_table`).
5. **Run round-trip + drift + pairwise tests** — all green is required
   before merging:
   ```bash
   pytest tests/db_pg/test_alembic_skeleton.py tests/db_pg/test_alembic_roundtrip.py
   ```
6. **Add a `MigrationTask` to `scripts/migrate_duckdb_to_pg/__init__.py:TASKS`**
   so the DuckDB→PG bulk copy includes the new table. If the table has
   JSONB columns that come from DuckDB JSON, add the (table, column)
   pair to `_JSON_COLUMNS` so the INSERT casts correctly.
7. **Build the PG repository** under `src/repositories/<name>_pg.py`,
   mirroring the existing DuckDB repository's public surface. Reuse
   helpers from the DuckDB module where shape-compatible (e.g.
   `table_registry_pg` imports `_encode_primary_key` from the DuckDB
   module).
8. **Write PG-side tests** under `tests/db_pg/test_<cluster>_pg.py`
   covering at minimum: CRUD round-trip, unique/composite constraint
   enforcement, and any cluster-specific invariants (e.g. system-group
   protection, ON CONFLICT semantics).

## The four load-bearing tests

These run on every PR and protect against the most dangerous mistakes:

  - `test_baseline_upgrade_creates_only_alembic_version` — baseline must
    never grow user tables.
  - `test_full_chain_roundtrip` — `upgrade head → downgrade base → upgrade head`
    preserves the schema bit-for-bit. Catches a missing or broken
    `downgrade()` body.
  - `test_pairwise_roundtrip` — for every (N, N+1) pair, `upgrade N →
    upgrade N+1 → downgrade N` restores the schema. Catches single-step
    botches that the full-chain test might mask.
  - `test_no_model_migration_drift` — `Base.metadata` matches `head`
    schema, no extras on either side. Catches forgotten migrations and
    model/SQL drift.

## DuckDB → Postgres data migration

`scripts/migrate_duckdb_to_pg/` copies app state from a DuckDB
`system.duckdb` file into Postgres. The migration is **idempotent** —
re-running is safe via `INSERT ... ON CONFLICT (pk) DO NOTHING`.

```bash
# Dry-run first against a snapshot copy
python -m scripts.migrate_duckdb_to_pg \
  --duckdb-path /var/agnes-snapshot/system.duckdb \
  --dry-run --verbose

# Live migration
AGNES_DB_URL="postgresql+psycopg://..." \
  python -m scripts.migrate_duckdb_to_pg \
  --duckdb-path /var/agnes-snapshot/system.duckdb

# Single-table verification
python -m scripts.migrate_duckdb_to_pg --only users --verbose
```

The CLI returns exit code 1 if any post-copy validation fails (row count
mismatch or PK-set checksum diverged).

## Backend choice for tests

### Local testing

The PG test suite (`tests/db_pg/`) runs against `pgserver` by default — that
ships a Postgres 16 binary inside its wheel, so no Docker or system PG is
required. To run against a real container instead:

```bash
AGNES_TEST_PG_BACKEND=container .venv/bin/pytest tests/db_pg/  # needs Docker
AGNES_TEST_PG_BACKEND=embedded  .venv/bin/pytest tests/db_pg/  # needs system postgres
```

CI matrices that want higher fidelity should set `container` explicitly.

| `AGNES_TEST_PG_BACKEND=` | Where it gets PG | When to use |
|---|---|---|
| _(unset)_ / `pgserver` | userland-bundled Postgres 16 from the `pgserver` PyPI package | default — works on any dev box, no install needed |
| `container` | testcontainers boots `postgres:16-alpine` via Docker | CI or local fidelity runs with Docker |
| `embedded` | pytest-postgresql + system `postgres` binary | dev laptop with Postgres installed |

## Future improvements

Cheap upgrades that the project research validated but hasn't yet landed:

- **`atlas migrate lint` CI step** — Atlas reads the Alembic-generated
  SQL and surfaces lock risks, missing indexes on FK columns, and unsafe
  column drops *before* a migration ships. Pure linter; doesn't replace
  Alembic. 30-min wire-up.
- **Expand/contract branch discipline (OpenStack pattern)** — Alembic
  supports named branches; the dual-write window during repository
  cutover maps directly. See
  https://docs.openstack.org/neutron/latest/contributor/alembic_migrations.html
  No new tooling required.
- **pgroll** for *post-migration* zero-downtime schema evolution. Defer
  until pgroll hits v1.0 and a Python SDK lands; for the one-time DuckDB
  cutover, the Alembic + OpenStack pattern is sufficient.
