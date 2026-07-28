# Postgres app-state follow-up — design

**Status:** brainstorm (approved by zsrotyr 2026-05-27), not yet implemented
**Date:** 2026-05-27
**Author:** zsrotyr
**Predecessor:** [PR #388](https://github.com/keboola/agnes-the-ai-analyst/pull/388) (`feat(db): introduce Postgres app-state layer alongside DuckDB`) — merged with 7-commit cleanup pass, see `docs/superpowers/plans/2026-05-26-pr-388-cleanup.md`.

## Problem

PR #388 ships **28 Postgres repository mirrors** for the app-state tables that existed when Vojta cut his branch (2026-05-15). Five DuckDB-only repositories landed on `main` after the cut and have no PG sibling:

| DuckDB-only repo | Purpose | LOC |
|---|---|---|
| `data_packages.py` | Curated data-package metadata (UI Browse + /catalog) | ~351 |
| `memory_domains.py` | Memory/knowledge domain registry + item membership | ~300 |
| `memory_domain_suggestions.py` | Non-admin domain proposals (pending → approved/rejected) | ~105 |
| `recipes.py` | Curated analysis recipes (CRUD with soft-delete) | ~161 |
| `user_stack_subscriptions.py` | Per-user subscription to stack items | ~97 |

Combined with PR #388's coverage gap, the deployment story is **partial cutover**: if an operator flips `DATABASE_URL`, the factory routes 28 repos to PG but these 5 still hit DuckDB through direct `XYZRepository(conn)` imports. That defeats the *"DuckDB only for analytics"* goal at the runtime layer for endpoints like `/catalog`, `/home`, `/admin/tables`, `/api/memory/*`, `/api/recipes/*`.

Beyond code, deployment ergonomics need a nudge: PR #388 ships `docker-compose.postgres.yml` as an **opt-in overlay**. For standard Agnes customers running on the existing `customer-instance` GCP VM, the path to enable PG is non-obvious (manual env wiring, no Secret Manager binding, no auto-migrate of existing DuckDB rows). This PR makes Postgres-side-car the **standard deploy** while keeping Cloud SQL as a no-friction operator override.

## Approach

Single omnibus PR adding code parity for the 5 missing repos, schema migration, contract tests, and the TF/compose wiring needed for first-deploy auto-migration of existing DuckDB rows.

```
keboola/agnes-the-ai-analyst (single PR ~2.6k LOC)
│
├── Sub-project A — code parity (5 repos)
│   ├── src/repositories/data_packages_pg.py            ← mirror
│   ├── src/repositories/memory_domains_pg.py           ← mirror
│   ├── src/repositories/memory_domain_suggestions_pg.py
│   ├── src/repositories/recipes_pg.py
│   ├── src/repositories/user_stack_subscriptions_pg.py
│   ├── src/repositories/__init__.py                    ← +5 factory funcs
│   ├── src/models/data_packages.py                     ← new cluster
│   ├── src/models/recipes.py                           ← new cluster
│   ├── src/models/knowledge.py                         ← extend (+3 tables)
│   ├── src/models/store.py                             ← extend (+1 table)
│   ├── migrations/versions/0011_data_packages_and_memory_extensions.py
│   ├── tests/db_pg/test_<repo>_pg.py                   ← 5 integration files
│   ├── tests/db_pg/test_<repo>_contract.py             ← 5 cross-engine parity
│   └── (callsite swap) app/web/router.py, app/api/memory.py, etc.
│
└── Sub-project B — TF/compose wiring
    ├── infra/modules/customer-instance/main.tf         ← +Secret Manager triple for PG password
    ├── infra/modules/customer-instance/startup-script.sh.tpl  ← pull secret, COMPOSE_FILE env
    ├── docker-compose.postgres.yml                     ← +data-migrate one-shot, volume bind to /data
    └── docs/postgres-cutover-runbook.md                ← ops playbook
```

### Why one PR (not three)

Three sub-projects (`5 repos`, `TF wiring`, `cutover automation`) are tightly coupled at the deploy boundary: TF without code = broken endpoints; code without TF = manual operator work. Shipping together gives one merge cycle, one version bump, one release-cut decision. PR #388 set the bar at 30k LOC; 2.6k is well under that ceiling.

### Why Postgres side-car as standard (not Cloud SQL HA)

Agnes is single-VM per customer. Multi-VM HA isn't in scope. A side-car Postgres on the same VM gives:

- **Persistence via existing `data` disk** + daily backup policy (`google_compute_disk_resource_policy_attachment "data_backup"`).
- **Zero new TF resources** beyond Secret Manager triple for PG password.
- **No cross-network latency** between app and PG (loopback).
- **Single restart** for app+migrate+postgres updates (via `agnes-auto-upgrade.sh`).

Operators who want managed/HA Postgres set `DATABASE_URL` in `.env` to point at Cloud SQL (or RDS/Supabase/Neon) — the overlay's side-car postgres + data-migrate then run against the external instance instead.

## Detailed design

### Sub-project A — code parity

#### Model split (per-domain cluster, matches PR #388 convention)

| Model file | Tables | New / extend |
|---|---|---|
| `src/models/data_packages.py` | `data_packages`, `data_packages_tables` (bridge) | NEW |
| `src/models/knowledge.py` | (+) `memory_domains`, `memory_domain_items` (bridge), `memory_domain_suggestions` | extend |
| `src/models/recipes.py` | `recipes` | NEW |
| `src/models/store.py` | (+) `user_stack_subscriptions` | extend |

Memory/recipes are conceptually adjacent to existing knowledge subsystem; user_stack_subscriptions is store/marketplace. Naming follows Vojta's clustering (audit/config/knowledge/lookup/misc/ops/rbac/store/telemetry), not per-table.

#### Per-repo `*_pg.py` modules

Mechanical mirror of DuckDB sibling. Adaptations per Vojta's dialect rules already established in PR #388:

- `INSERT OR IGNORE` → `INSERT ... ON CONFLICT (pk_cols) DO NOTHING`
- `INSERT OR REPLACE` → `INSERT ... ON CONFLICT (pk_cols) DO UPDATE SET ...`
- DuckDB `list_contains(arr, 'x')` → PG `'x' = ANY(arr)` or `arr @> '["x"]'::jsonb` per column type
- `json.dumps(tags)` in DuckDB INSERT → JSONB native via psycopg adaptation; explicit `CAST(:tags AS JSONB)` in SQL parameter binding
- Soft-delete `deleted_at IS NULL` filter — identical semantics across engines

JSONB columns by repo (used by `_JSON_COLUMNS` allowlist):

- `data_packages.tags`, `data_packages.when_to_use`, `data_packages.when_not_to_use`, `data_packages.example_questions`
- `memory_domains` — none currently (slug + name + icon + color all scalar)
- `memory_domain_suggestions` — none
- `recipes.tags` — JSONB
- `user_stack_subscriptions` — none

#### Factory entries in `src/repositories/__init__.py`

```python
__all__ = [
    ...,  # 28 existing
    "data_packages_repo",
    "memory_domains_repo",
    "memory_domain_suggestions_repo",
    "recipes_repo",
    "user_stack_subscriptions_repo",
]

def data_packages_repo() -> Any:
    if use_pg():
        from src.repositories.data_packages_pg import DataPackagesPgRepository
        return DataPackagesPgRepository(_pg_engine())
    from src.repositories.data_packages import DataPackagesRepository
    return DataPackagesRepository(get_system_db())
# ... 4 more
```

#### Callsite swap

`app/web/router.py`, `app/api/memory.py`, `app/api/data_packages.py` (if it exists), `cli/commands/*` — replace direct `XYZRepository(conn)` imports with factory calls. Estimated **~10–15 callsites total** across 5 repos (smaller than PR #388 because these repos are less heavily threaded into the API surface).

#### Alembic revision

Single omnibus revision `migrations/versions/0011_data_packages_and_memory_extensions.py` matching Vojta's clustering convention (`0009_store.py` covers 6 tables, `0010_knowledge.py` covers 6):

```python
revision: str = "0011_data_packages_and_memory_extensions"
down_revision: str | None = "0010_knowledge"

def upgrade() -> None:
    op.create_table("data_packages", ...)              # 17 cols
    op.create_table("data_packages_tables", ...)       # bridge, FK to data_packages(id) + table_registry(id)
    op.create_table("memory_domains", ...)              # 9 cols
    op.create_table("memory_domain_items", ...)         # bridge, FK to memory_domains(id) + knowledge_items(id)
    op.create_table("memory_domain_suggestions", ...)   # 7 cols + idx on status
    op.create_table("recipes", ...)                     # ~9 cols
    op.create_table("user_stack_subscriptions", ...)    # ~6 cols + idx on (user_id, resource_type, resource_id)

def downgrade() -> None:
    # Reverse order — bridges before parents to satisfy FK
    op.drop_table("user_stack_subscriptions")
    op.drop_table("recipes")
    op.drop_table("memory_domain_suggestions")
    op.drop_table("memory_domain_items")
    op.drop_table("memory_domains")
    op.drop_table("data_packages_tables")
    op.drop_table("data_packages")
```

#### Contract tests (5 files, 52 parametrized tests)

Match audit/users/RBAC/store pattern shipped in PR #388. Per repo:

| Repo | Cases × backends | Notes |
|---|---|---|
| `test_data_packages_contract.py` | 6 × 2 = 12 | CRUD, soft-delete, bridge add/remove, slug-unique, list filter, list_member_ids_bulk |
| `test_memory_domains_contract.py` | 6 × 2 = 12 | CRUD, soft-delete, item membership, slug-unique, list_items_of_domain, list_domains_of_item |
| `test_memory_domain_suggestions_contract.py` | 4 × 2 = 8 | create, list pending, resolve (approve/reject), count_pending |
| `test_recipes_contract.py` | 5 × 2 = 10 | CRUD, soft-delete, slug-unique, list with search, hard_delete |
| `test_user_stack_subscriptions_contract.py` | 5 × 2 = 10 | subscribe, unsubscribe, is_subscribed, list_for_user, list_users_subscribed_to |

After merge: **240 (PR #388) + ~52 contract + ~25 per-repo integration = ~317 PG tests passing**.

### Sub-project B — TF/compose wiring

#### Secret Manager triple (matches existing `jwt` pattern)

In `infra/modules/customer-instance/main.tf`, add 4 resources (analog of lines 35–71 for JWT):

```hcl
resource "random_password" "postgres" {
  length  = 32
  special = false  # PG-friendly, avoids shell-escape headaches in startup-script
}

resource "google_secret_manager_secret" "postgres" {
  secret_id = "agnes-postgres-${var.name}"
  replication { auto {} }
}

resource "google_secret_manager_secret_version" "postgres" {
  secret      = google_secret_manager_secret.postgres.id
  secret_data = random_password.postgres.result
}

resource "google_secret_manager_secret_iam_member" "vm_postgres" {
  secret_id = google_secret_manager_secret.postgres.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.vm.email}"
}
```

#### Startup-script changes

```bash
JWT_SECRET="$(gcloud secrets versions access latest --secret=agnes-jwt-${name})"
POSTGRES_PASSWORD="$(gcloud secrets versions access latest --secret=agnes-postgres-${name})"  # NEW

mkdir -p /data/postgres
chown 70:70 /data/postgres        # postgres:16-alpine container uid
chmod 700 /data/postgres

cat > /opt/agnes/.env <<EOF
JWT_SECRET_KEY=$JWT_SECRET
POSTGRES_PASSWORD=$POSTGRES_PASSWORD
DATABASE_URL=postgresql+psycopg://agnes:$POSTGRES_PASSWORD@postgres:5432/agnes
COMPOSE_FILE=docker-compose.yml:docker-compose.postgres.yml
# ... existing other env
EOF
chmod 600 /opt/agnes/.env

cd /opt/agnes && docker compose up -d
```

#### Compose overlay updates

Add **`data-migrate` one-shot service** to `docker-compose.postgres.yml` + bind named volume to `/data/postgres` for backup-policy coverage:

```yaml
services:
  postgres: { ... existing ... }
  migrate:  { ... existing ... alembic upgrade head ... }

  data-migrate:
    build: .
    command: python -m scripts.migrate_duckdb_to_pg --duckdb-path /data/state/system.duckdb
    depends_on:
      postgres: { condition: service_healthy }
      migrate:  { condition: service_completed_successfully }
    environment:
      - DATABASE_URL=postgresql+psycopg://agnes:${POSTGRES_PASSWORD:-agnes}@postgres:5432/agnes
    volumes:
      - data:/data:ro          # source DuckDB read-only
    restart: "no"

  app:
    depends_on:
      postgres:     { condition: service_healthy }
      migrate:      { condition: service_completed_successfully }
      data-migrate: { condition: service_completed_successfully }    # NEW gate

volumes:
  postgres_data:
    driver: local
    driver_opts:
      type:   none
      device: /data/postgres   # bind named volume to backed-up data disk
      o:      bind
```

#### Rollback runbook

`docs/postgres-cutover-runbook.md` — operator-facing markdown with:

1. **What changed** (1-paragraph: side-car PG default, Cloud SQL opt-in, auto-migrate)
2. **Verify after deploy** (`/api/health` 200, `agnes catalog` non-empty, audit_log growth visible in PG)
3. **Cloud SQL operator override** (unset COMPOSE_FILE PG overlay path, set `DATABASE_URL` to external PG)
4. **Rollback to DuckDB** (remove `COMPOSE_FILE` + `DATABASE_URL` from `.env`, restart compose; DuckDB file untouched)
5. **POSTGRES_PASSWORD rotation** (manual `gcloud secrets versions add` + restart compose)
6. **Troubleshooting** (data-migrate hung → kill + restart, idempotent; disk full → df /data, prune old logs)

## Testing matrix (per layer from project memory)

| Layer | Coverage | This PR contribution |
|---|---|---|
| A — Static | PG unit + DuckDB regression + contract tests | +52 contract + +25 integration tests |
| B — Pre-cutover dynamic | Live E2E walkthrough on dev VM after deploy | runbook step in postgres-cutover-runbook.md |
| C — Cutover drill | `data-migrate` against real prod-snapshot DuckDB | already wired (uses existing PR #388 script + new 5 tables auto-discovered) |
| D — Failure modes | PG crash, kill data-migrate, disk full | enumerated in risk register; mitigations in compose + startup-script |
| E — Rollback drill | Remove env, restart, verify DuckDB path active | documented in runbook |
| F — Observability | pg_stat_activity, audit_log growth, /data/postgres disk usage | no instrumentation added — operator-driven |

## Risk register

| # | Risk | Likelihood | Mitigation |
|---|---|---|---|
| R1 | data-migrate slows first deploy boot (large DuckDB) | Medium | Idempotent, observable in compose logs; `--only <table>` for ops triage |
| R2 | `/data/postgres` fills disk silently | Low–Medium | Existing `df` monitoring on `data` disk; no app-level alert added (out of scope) |
| R3 | Operator forgets to bump infra repo module pin → existing VMs miss new TF resources | Medium | `**BREAKING**` CHANGELOG bullet; runbook explicit step |
| R4 | Cloud SQL operator override path untested in this PR | Low | DATABASE_URL resolution is mechanically same as side-car; PR #388 covers URL handling |
| R5 | DuckDB ↔ PG contract drift in remaining 24 untested repos | Low | Contract tests now cover audit/users/RBAC/store/+5 new = 9/33 repos; remainder accepted gap |
| R6 | Rev 0011 fails on partially-migrated existing PG | Very Low | Alembic idempotent on partial state; downgrade() in revision file |
| R7 | Bridge tables FK fails (parent missing on rev 0011 upgrade) | Low | Migration creates parents first; downgrade in reverse order |
| R8 | psycopg JSONB cast fails on edge-case `tags` payloads | Low | `_JSON_COLUMNS` allowlist + contract tests cover round-trip; explicit override pathway exists |

## Out of scope

- **Retire 28 DuckDB state repos** — separate PR ≥2 weeks post-stability
- **Cloud SQL Terraform module** (`infra/modules/agnes-cloudsql/`) — operators set `DATABASE_URL` manually for now
- **Postgres HA replica / read-only fleet** — not in single-VM Agnes shape
- **PG version upgrade beyond 16** — separate process when 16 approaches EOL
- **Auto-rotation of POSTGRES_PASSWORD** — Secret Manager allows manual rotation; auto needs separate spec

## Implementation scope

| Component | Lines | New / extend |
|---|---|---|
| 5 `*_pg.py` modules | ~1010 | new |
| `src/models/data_packages.py`, `recipes.py` | ~120 | new |
| `src/models/knowledge.py`, `store.py` | +60 | extend |
| `migrations/versions/0011_*.py` | ~250 | new |
| `src/repositories/__init__.py` | +50 | extend (5 factory funcs) |
| Callsite swaps | ~30 | edit |
| `tests/db_pg/test_*_pg.py` (5) | ~400 | new |
| `tests/db_pg/test_*_contract.py` (5) | ~500 | new |
| `infra/modules/customer-instance/main.tf` | +30 | extend |
| `infra/modules/customer-instance/startup-script.sh.tpl` | +15 | extend |
| `docker-compose.postgres.yml` | +20 | extend |
| `docs/postgres-cutover-runbook.md` | ~150 | new |
| `CHANGELOG.md` | +5 | `**BREAKING**` bullet |
| **Total** | **~2640** | manageable PR |

## Conventions reinforced from CLAUDE.md

- **Vendor-agnostic OSS**: no customer-specific tokens in this PR. `customer-instance` module is generic; per-customer values (region, tier) stay in private infra repos.
- **CHANGELOG discipline**: single `**BREAKING**` bullet under `[Unreleased]` referencing the deploy-side change (postgres side-car becomes default).
- **Release-cut**: minor version bump (this PR adds new capability without removing existing path). Per `releaser-role` memory, **ask before minor** — the question for the merger is *"0.55.x → 0.56.0?"*.
- **No AI attribution** in commits or PR titles/bodies.
