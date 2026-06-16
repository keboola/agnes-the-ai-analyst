# Named Source Connections — Multi-Stack / Multi-Project Data Sources

**Date:** 2026-06-12
**Status:** Approved design, pre-implementation (brainstormed + decided 2026-06-12)
**Verified against:** `0.71.9` (HEAD `d2f10022`)
**Related:** RFC #461 (Universal MCP — the prior art this design generalizes)

## 0. Decisions (resolved at brainstorm)

| Decision | Outcome |
|---|---|
| Scope of MVP | **Generic** — multiple Keboola connections (projects and/or stacks) AND multiple BigQuery projects, both in the MVP |
| Table shape | One generic `source_connections` table + per-type `ConnectionSpec` validation; converging later with `mcp_sources` into a universal `sources` registry is an explicit future direction, not this change |
| Secrets | **Vault-first** (Fernet, shared cipher helpers from `app/secrets_vault.py`), `token_env` as legacy fallback — same dual path MCP sources use |
| Config authority | **Registry is the source of truth.** `instance.yaml` / env vars are a one-time first-boot seed; env vars are then deprecated in three steps (§3.4) and removed in a future BREAKING release |
| Vault table | New `connection_secrets` table (not a generalized `mcp_secrets`); shared cipher helpers keep later convergence cheap |
| Deletion semantics | **Block** deletion while `table_registry` rows reference the connection (409 + list of tables); explicit re-pointing required |

## 1. Context & goal

Agnes today assumes **exactly one connection per data-source type**: one
Keboola stack + one Storage token, one BigQuery project. A deployment that
wants tables from two Keboola projects (a Keboola token is per-project, so
"connection" = stack + project), two stacks, or two GCP projects cannot
express that.

The single-connection assumption is baked into four layers:

| Layer | Where | Limitation |
|---|---|---|
| Env/secrets | `KEBOOLA_STACK_URL`, `KEBOOLA_STORAGE_TOKEN` (`connectors/keboola/client.py`, `connectors/keboola/extractor.py` `__main__`) | one global URL + token |
| Instance config | `data_source.type` + `data_source.keboola` / `data_source.bigquery` objects (`config/instance.yaml.example`) | singular type, one config block per type |
| Registry | `table_registry` rows carry only `source_type + bucket + source_table` (`src/db.py`) | two projects with the same bucket name are indistinguishable |
| Extract layout | extractor writes to hard-coded `extracts/keboola/` (`connectors/keboola/extractor.py`, `app/api/sync.py`) | one `extract.duckdb` per source *type* |

A side irritation discovered the same day: the stack URL is consumed verbatim
in most paths (no trailing-slash normalization), and there are two env
spellings for the same value (`KEBOOLA_STACK_URL` vs `KBC_STACK_URL` in
`app/api/metadata.py`). Connection registration gives us a single choke point
to validate and normalize once.

**Goal:** first-class *named connections* — N connections per source type,
each with its own credentials and its own extract directory — without
breaking existing single-connection deployments.

## 2. Prior art already shipped (RFC #461)

The Universal MCP work introduced exactly the model we need, scoped to MCP
sources only:

- a registry of named sources (admin REST + UI + CLI), each writing to its
  own `extracts/<source.name>/` directory;
- the **secrets vault** (`app/secrets_vault.py`, Fernet under
  `AGNES_VAULT_KEY`): write-only shared-scope secrets (`mcp_secrets`, one row
  per source) + per-user scope (`mcp_user_secrets`), with a legacy
  `auth_secret_env` env-var fallback;
- the orchestrator needs **zero changes**: `SyncOrchestrator.rebuild()`
  already scans `extracts/*/extract.duckdb` generically.

This design lifts that pattern from "MCP only" to "any connector".

## 3. Design

### 3.1 Data model

```sql
CREATE TABLE source_connections (
    id          VARCHAR PRIMARY KEY,      -- uuid
    name        VARCHAR NOT NULL UNIQUE,  -- slug; doubles as extracts/<name>/ dir
    source_type VARCHAR NOT NULL,         -- 'keboola' | 'bigquery' | ...
    config      TEXT NOT NULL,            -- JSON: {stack_url, project, location,
                                          --        billing_project, max_bytes_per_materialize, ...}
    token_env   VARCHAR,                  -- legacy/ops fallback, like MCP auth_secret_env
    is_default  BOOLEAN DEFAULT FALSE,    -- at most one per source_type (repo-enforced, both backends)
    created_by  VARCHAR,
    created_at  TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE connection_secrets (
    connection_id VARCHAR PRIMARY KEY,    -- soft ref to source_connections.id
    ciphertext    TEXT NOT NULL,
    updated_at    TIMESTAMP DEFAULT current_timestamp
);
```

- Per-type validation of the `config` JSON lives in a small `ConnectionSpec`
  registry in the connector package — mirroring how `app/resource_types.py`
  registers `ResourceTypeSpec`s; adding a source type needs no migration.
- `connection_secrets` reuses the Fernet cipher helpers from
  `app/secrets_vault.py`. Write-only contract: the API only ever reports
  `has_vault_secret`; the value is never read back out.
- `table_registry` gains nullable `connection_id`. `NULL` means "the default
  connection for this row's `source_type`" — every existing row keeps working
  untouched. Soft reference (no FK), integrity enforced in the repo layer,
  consistent with the rest of the schema.
- URL normalization (`rstrip("/")`, scheme check) happens at registration
  time, so consumers never see a trailing slash again.
- BigQuery: credentials are **not stored** — ADC (VM service account) as
  today. `billing_project` and `max_bytes_per_materialize` move into the
  connection's `config` (per-connection cost guardrail instead of global).
- Migration ships in **both ladders in the same PR**: DuckDB `_vN_to_v(N+1)`
  in `src/db.py` + matching Alembic step. New repo pair
  `src/repositories/source_connections.py` + `source_connections_pg.py`,
  factory entry, cross-engine contract test.

### 3.2 Resolution & data flow

- Resolution order at extraction/sync/query time: `row.connection_id` →
  that connection; `NULL` → the default connection for the row's
  `source_type`; no connection registered at all → legacy env/yaml path
  (deprecation window only, with a startup WARNING).
- Token resolution per connection: **vault → `token_env` → (seeded default
  only) legacy `KEBOOLA_STORAGE_TOKEN`**.
- `connectors/keboola/extractor.py __main__` and the sync-trigger path in
  `app/api/sync.py` group registry rows by resolved connection, run one
  extraction pass per connection, and write to `extracts/<connection.name>/`.
  The materialized path resolves credentials through the connection.
- `_meta` contract unchanged. `_remote_attach` gains an optional
  `connection` column — when present, the orchestrator resolves the token
  through the connection (vault) instead of a pure `token_env` lookup;
  extracts without the column keep using the env path.
- Orchestrator: no change (scans `extracts/*/extract.duckdb` generically).

### 3.3 Admin surface

- **REST**: `GET/POST/PATCH/DELETE /api/admin/connections` +
  `PUT/DELETE /api/admin/connections/{id}/secret` (write-only). All gated
  `Depends(require_admin)`; mutations write `audit_log` entries.
  `POST /api/admin/register-table` accepts optional `connection` (name or id).
- **UI**: new `/admin/connections` page (list + detail, page-shell template).
  Detail reuses the "Vault secret" card pattern from
  `admin_mcp_source_detail.html` (set/rotate/clear + `has_vault_secret`
  status) and adds a test-connection button (Keboola: token verification
  against the Storage API; BigQuery: dry-run via ADC). `/admin/tables`
  registration form gains a connection dropdown — **hidden while only one
  connection exists**, so single-stack deployments pay zero UI tax.
- **CLI**: `agnes admin connection list/add/update/remove/secret set/secret
  clear` + `--connection` on table registration.
- **MCP**: corresponding admin tools (the REST × CLI × MCP coverage ratchet
  enforces this anyway).

### 3.4 Seeding & env-var deprecation

On first boot with no connections, seed from env/config (normalized):

```
name='keboola',  source_type='keboola',  is_default=TRUE,
  config={stack_url: <env/yaml value>}, token_env='KEBOOLA_STORAGE_TOKEN'
name='bigquery', source_type='bigquery', is_default=TRUE,
  config={project, location, billing_project, ...}
```

`name='keboola'` / `name='bigquery'` deliberately match the existing extract
directories — **no data move, no re-extraction** on upgrade. Seeding is
one-time; afterwards the registry is the sole source of truth.

Deprecation in three steps:
1. **Now:** startup WARNING when `KEBOOLA_STACK_URL`/`data_source.*` is set
   but a registry connection exists ("value ignored; managed in
   /admin/connections").
2. **Docs/CHANGELOG:** migration recipe to vault
   (`agnes admin connection secret set keboola`); `instance.yaml.example`
   marks `data_source.*` blocks "initial seed, managed in /admin/connections
   afterwards".
3. **Future BREAKING release:** remove the env fallback entirely, together
   with the duplicate `KBC_STACK_URL` spelling.

### 3.5 Error handling

- Unknown/deleted connection on a registry row → per-table error in
  `summary.errors` (existing pattern); other tables keep syncing.
- `DELETE /api/admin/connections/{id}` with referencing tables → 409 with
  the list of table names.
- Storing a secret without `AGNES_VAULT_KEY` → 400 `vault_key_not_configured`
  (same as MCP).
- Vault key lost/rotated → connection detail shows "secret unreadable,
  re-enter".

## 4. Testing obligations

- Cross-engine contract test `tests/db_pg/test_source_connections_contract.py`
  (DuckDB + PG); status-parity sweep picks up the new admin routes.
- Extractor unit tests with two fake connections — separate `extracts/`
  directories AND separate tokens verified.
- E2E: register a second connection → register a table on it → sync → view
  lands in `analytics.duckdb`.
- Seeding regression test: env → registry, idempotent across restarts.
- Full suite before every push: `.venv/bin/pytest tests/ --tb=short -n auto -q`.

## 5. Phasing (one PR each, each with its CHANGELOG bullet)

| Phase | Scope |
|---|---|
| 1 | Schema (both ladders) + repos + contract tests + first-boot seeding |
| 2 | Keboola extraction/sync per connection (`extracts/<name>/`, vault/token resolution, `_remote_attach.connection`) |
| 3 | BigQuery per connection (per-connection `billing_project` + cost guardrail) |
| 4 | Admin REST + UI + CLI + MCP tools + vault secret endpoints |
| 5 | Docs + deprecation warnings + `KBC_STACK_URL` cleanup |

Phases 1–4 are the MVP (BigQuery is in MVP per the brainstorm decision);
phase 5 can trail. Each phase keeps the suite green on its own.

## 6. Out of scope

- Trailing-slash hotfix at the existing consumers (`rstrip("/")`) — small
  independent PR worth shipping immediately; this design later makes it
  redundant for registry-managed connections.
- Cross-connection JOIN semantics — nothing new needed; all connections land
  as views in the same `analytics.duckdb`.
- Per-user data-source credentials (the `mcp_user_secrets` analog) — YAGNI
  until a concrete need; the vault scope split already accommodates it.
- Unifying `source_connections` with `mcp_sources` into one `sources`
  registry — explicit future direction; this design keeps the cipher helpers
  and the `has_vault_secret` contract shared so that convergence stays cheap.
