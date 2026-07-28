# Semantic-layer sources: per-connection master token, multi-project sync, `/admin/semantic-layer`

**Date:** 2026-07-28
**Status:** Approved design, pre-implementation

## Problem

The Keboola semantic-layer sync (`connectors/keboola/semantic_layer.py`) requires a
master (owner) Storage API token — the Metastore API rejects non-master tokens with
an opaque error. Today there is nowhere to configure that token except by replacing
the connection's regular storage token (over-privileging every table sync) or the
legacy `KEBOOLA_STORAGE_TOKEN` env slot. Additionally:

- The sync reads exactly **one** Keboola project (the default/first
  `source_connections` row); instances with multiple Keboola connections cannot
  sync more than one semantic layer.
- Provenance on synced rows is the single global string
  `source='keboola_semantic_layer'` — if two projects ever synced, each run's
  prune pass would delete the other project's rows.
- The admin surface for semantic-layer state is a small summary block on
  `/admin/data-sources`, with no per-source breakdown.

## Goals

1. An admin can set an **optional master token per Keboola connection**, in the UI,
   validated at save time.
2. The semantic-layer sync runs across **all** Keboola connections that have a
   master token, with per-connection provenance and prune isolation.
3. A dedicated **`/admin/semantic-layer`** page shows per-source status and counts,
   designed so future non-Keboola semantic sources (e.g. an OpenMetadata metrics
   import) appear as additional row types without restructuring.

## Non-goals (explicitly out of scope)

- OpenMetadata **metrics import** (`connectors/openmetadata/client.py::get_metrics`
  exists but mapping OM metrics onto registered tables has open questions). The
  page and provenance model must accommodate it later; nothing is built now.
- Per-source refresh buttons (the refresh endpoint stays global single-flight).
- A new "semantic source" registry entity — sources are **derived** from existing
  configuration (Keboola connections with a master token).

## Design

### 1. Master token per connection

- **Storage:** existing `connection_secrets` vault, new derived key
  `{connection_id}:master`. Both backends (DuckDB `app/secrets_vault.py`, PG
  `src/repositories/secrets_vault_pg.py`) key secrets by plain string → **no
  schema migration** for the vault.
- **API:** extend `PUT /api/admin/source-connections/{id}/secret` and
  `DELETE …/secret` with an optional `kind` field: `"storage"` (default) |
  `"master"`. Existing callers are unaffected.
  - `kind=master` is only valid for `source_type='keboola'` connections → 400
    otherwise.
  - On `PUT` with `kind=master`, the server immediately calls
    `verify_token()` against the connection's stack URL and rejects tokens where
    `isMasterToken` is false (400 with the same actionable message
    `require_master_token` produces). This turns the previously post-hoc sync
    failure into an at-save validation. Three hard requirements on this new
    outbound-call site (review findings):
    - Call `_validate_stack_url(config, required=True)` immediately before
      constructing the client — `create_connection`/`update_connection` do NOT
      validate the host, so this save path must close the SSRF window itself,
      mirroring the existing `/test` and `/tables` handlers.
    - `verify_token()` is a blocking `requests` call — run it via
      `run_in_threadpool` (or an async client with a bounded ~10s timeout,
      matching `/test`), never bare inside the async handler.
    - Any exception text surfaced in the error response or logs goes through the
      client's `_redact()` helper — never bare `str(exc)` (the freshly typed
      token is in flight on this path).
  - `GET` connection responses gain `has_master_secret: bool` (alongside the
    existing secret-status fields from `_with_secret_status`), implemented via
    the vault repo's `.has()` — never `.get()` — per the vault's own API-layer
    contract.
  - `DELETE /api/admin/source-connections/{id}` (connection delete) also deletes
    the `{id}:master` vault row, or the ciphertext is orphaned forever.
- **CLI parity:** the existing `agnes admin connection` command group gains a
  `--kind master` option on its secret-setting path (same PR). MCP exposure is
  deliberately absent — a secret-writing endpoint must not be LLM-callable — and
  that carve-out is intentional, not an omission.
- **UI (`/admin/data-sources` connection card):** next to the existing
  "Rotate token" control, a "Master token (semantic layer)" section — set/not-set
  badge, password input + Save, Remove. Tooltip explains what the master token
  unlocks and that it is optional. The connection-create wizard is unchanged
  (master token is added on the card after creation).
- **Secret handling rules** (per the security playbook): value travels only in the
  JSON body over the authed admin API, is never logged, never placed on argv or in
  URLs; the redaction helper in `connectors/keboola/metastore_client.py` already
  covers the token in error strings.

### 2. Provenance: `source_ref` column (migration v105 → v106)

- Add nullable `source_ref VARCHAR` to `metric_definitions` **and**
  `glossary_terms`.
  - DuckDB (`src/db.py` — four touch points, per the recurring dual-call-site
    footgun in this file): the `_v105_to_v106` step function (template:
    `_v104_to_v105`), its call in the **sequential upgrade branch**
    (`if current < 106: …`), its call in the **fresh-install/self-heal replay
    branch** (unconditional chronological list), and the snapshot DDL of **both**
    tables (`metric_definitions`, `glossary_terms`) declaring `source_ref
    VARCHAR` directly so the step is a no-op on fresh installs.
  - PG: matching Alembic revision (clone the latest revision as template) **and**
    `source_ref: Mapped[str | None]` on both `MetricDefinition` and
    `GlossaryTerm` in `src/models/config.py` — `tests/db_pg/test_schema_parity.py`
    diffs DuckDB columns against the ORM models, not the Alembic file (pattern
    precedent: `source_ref` in `src/models/knowledge.py`).
  - Both ladders reach the same endpoint; `tests/test_db_schema_version.py`
    gates it.
- Semantics: for rows with `source='keboola_semantic_layer'`, `source_ref` is the
  originating `source_connections.id`. For `manual` / `yaml_import` rows it stays
  NULL and is ignored.
- **Legacy-row adoption:** pre-existing semantic rows have `source_ref IS NULL`.
  The sync pass for the **default** Keboola connection treats NULL-ref rows as its
  own: upserts stamp `source_ref`, and its prune scope is
  `(source='keboola_semantic_layer' AND (source_ref = :id OR source_ref IS NULL))`.
  Non-default connections' prune scope is strictly `source_ref = :id`. Result: an
  upgrade produces no duplicates and no cross-source deletion.
- Repository changes (`src/repositories/metrics.py`/`metrics_pg.py`,
  `src/repositories/glossary.py`/`glossary_pg.py`) land in the same PR for
  **both** backends. New methods required by the conflict policy (§3): a
  `find_by_name`-style exact-match lookup on metrics and an exact-`term` lookup
  on glossary (today only `search()`/ILIKE exists), both backends, with
  NULL-safe ownership comparison (`source_ref IS DISTINCT FROM :ref`, never
  `!=`, so legacy NULL-ref rows compare correctly).
- Contract tests: `tests/db_pg/test_glossary_contract.py` already has a
  `["duckdb", "pg"]`-parametrized `repo` fixture — extend it for `source_ref`
  round-trip + scoped listing. For **metrics** no such fixture exists
  (`tests/db_pg/test_config_pg.py` is PG-only smoke) — add a parametrized
  fixture there mirroring the glossary contract pattern so the new behavior is
  genuinely cross-engine. Signature drift between the repo pairs is additionally
  caught by the AST-based `tests/db_pg/test_repo_method_parity.py`.
- All new code keeps routing through the factory (`metric_repo()`,
  `glossary_repo()`, `connection_secrets_repo()`) — no `get_system_db()` or
  direct repo instantiation (`tests/test_backend_split_guard.py` ratchet).

### 3. Multi-source sync loop

`sync_semantic_layer()` becomes an orchestrating loop; the existing single-project
logic is extracted into a per-source function taking
`(stack_url, token, source_ref)`.

- **Source enumeration:** all `source_type='keboola'` connections that have a
  `{id}:master` vault secret — default connection first, remaining sources in a
  deterministic secondary order (connection `id`), so first-claim outcomes in
  same-run conflicts are stable across runs.
- **Duplicate-project guard:** nothing prevents registering the same upstream
  project twice (unique constraint is on connection `name` only). Two
  connections syncing the same project would flip-flop `source_ref` ownership
  and cross-wipe each other every run. Therefore: each source's sync starts with
  `verify_token()` anyway — dedupe on the **project identity** it returns
  (owner/project id + stack host); if a later source resolves to a project an
  earlier source already synced this run, skip it and report it as
  `skipped_duplicate_project` in that source's result.
- **Backward-compat fallback:** if **no** connection has a master token, run
  today's single-source resolution (`_resolve_keboola_credentials`: explicit args →
  legacy `KEBOOLA_STACK_URL`/`KEBOOLA_STORAGE_TOKEN` full pair → default
  connection's regular token), with `source_ref` = default connection id when the
  credentials came from a connection, else NULL. Fallback prune scope is
  `source_ref IS NULL OR source_ref = <default connection id>` — **never** all
  semantic rows: if multi-source rows from other connections exist (an admin
  removed the last master token after multi-source syncs), those rows are left
  **orphaned-but-intact** rather than silently deleted, and the
  `/admin/semantic-layer` page surfaces them as orphaned sources with a hint to
  re-add a master token or clean up manually. Behavior for existing
  installations is unchanged, including the master-token preflight error
  message.
- **Precedence note:** when at least one master token exists, master-token sources
  are the *only* sources — the legacy env pair is not additionally synced (it has
  no identity to scope a prune to; mixing it in would recreate the cross-wipe
  problem).
- **Name-conflict policy:** metric names must stay unique for catalog UX. Row
  `id`s already embed the model UUID (`keboola/{model_uuid}/{name}`), so id
  collisions across projects cannot happen — the conflict check is by **name**,
  via the new exact-match repo lookups (§2). If an incoming metric's name
  already exists with a different `source_ref` (NULL-safe comparison) or a
  non-semantic `source`, the row is **skipped**, counted as `skipped_conflict`,
  and reported per source. Ownership is sticky (the source whose ref is already
  on the row keeps it), so sync order does not decide outcomes beyond the
  deterministic first claim. Same policy for glossary terms (exact `term`
  lookup).
- **Prune safety valve per source:** the existing "upstream returned zero models
  while rows exist → skip prune, log loudly" guard currently computes `existing`
  over ALL semantic rows. It must be scoped to the same per-`source_ref` set as
  that source's prune (NULL-inclusive for the default connection) — otherwise
  one source's empty response is masked by other sources' row counts, or its
  legitimate prune is blocked by them.
- **Per-source error isolation:** one source failing (Storage/Metastore API error,
  non-master token that slipped in) records an error for that source and continues
  with the rest. `MasterTokenRequiredError` is no longer fatal to the whole run
  when other sources exist — it is captured into that source's result.
- **Result shape:** the endpoint's response and the in-memory
  `get_last_refresh_summary()` state gain a `sources: [{connection_id, name,
  status, created_or_updated, pruned, skipped_conflict, error?, …}]` breakdown
  while keeping the existing top-level aggregate fields (existing consumers keep
  working).
- `POST /api/admin/run-keboola-semantic-layer-refresh` keeps its route, auth
  (`require_admin` + scheduler-token path) and single-flight lock.

### 4. `/admin/semantic-layer` page

- Admin-only web route (same auth pattern as the other `/admin/*` pages), template
  extends **`base_page.html`** (gradient hero + toolbar + page blocks), context
  spread from `_chrome_ctx(request, user)`. Page CSS in `{% block head_extra %}`,
  `ds.*` macros, `var(--ds-*)` tokens only — the design-system contract tests
  (`tests/test_design_system_contract.py`) apply and specifically reject
  `.container:has()` opt-outs, bare `:root{}`, raw `#hex` colors, and
  `var(--primary)`.
- Content:
  - A sources table: connection name, stack URL host, master-token badge, last-run
    status per source (from the refresh summary), metric + glossary counts per
    `source_ref`, error message when the last run failed.
  - Rows are rendered from a generic "semantic source" view-model
    `{type, label, detail, status, counts}` so a future `openmetadata` source type
    is a new row producer, not a page rewrite.
  - One global **Refresh now** button (calls the existing refresh endpoint;
    disabled with a hint while a run is in flight / 409).
  - Empty state: no connection has a master token → callout explaining the master
    token requirement with a link to `/admin/data-sources`.
  - Orphaned sources: rows whose `source_ref` matches no currently-enumerated
    source (see fallback-mode prune in §3) are listed with their counts and a
    hint to re-add a master token or delete the rows manually.
- `/admin/data-sources` keeps only a one-line semantic-layer status with a link to
  the new page (replacing the current multi-line summary block).
- Navigation: the page is linked from the admin nav the same way sibling admin
  pages are registered.

### 5. Testing

- **Resolution/loop:** multi-source enumeration + deterministic ordering;
  fallback path when no master token; legacy pair not synced alongside master
  sources; duplicate-project dedupe (`skipped_duplicate_project`).
- **Prune isolation:** two sources, deleting upstream metrics from one never
  touches the other's rows; NULL-ref adoption by the default connection;
  per-source safety valve (empty upstream for source A skips A's prune only);
  fallback mode leaves other sources' rows orphaned-but-intact.
- **Conflicts:** cross-source name collision → skip + counted, sticky ownership,
  NULL-ref rows owned by default connection in comparisons.
- **Endpoint:** `kind` validation (non-keboola 400, bad value 422/400), master
  verification on save (non-master 400, Storage API outage → structured error,
  stack-URL host validation rejected → 400, error text redacted),
  `has_master_secret` exposure, connection delete also removes the `{id}:master`
  vault row.
- **CLI:** `--kind master` path on the connection secret command.
- **Migration:** both ladders reach v106 (`tests/test_db_schema_version.py`),
  contract tests cover `source_ref` on both backends.
- **Page:** route smoke test (auth, renders, sources listed), design-system
  contract suite, screenshot during implementation.
- Full suite before every push (`.venv/bin/pytest tests/ --tb=short -n auto -q`).

## Rollout / compatibility

- No config file changes; no new env vars. Existing single-project installs see
  identical behavior until a master token is saved.
- CHANGELOG bullets under `[Unreleased]` (Added: master token + page + multi-source;
  Changed: data-sources summary block) in the same PR. At PR-open time apply the
  release-cut decision tree from `docs/RELEASING.md` (if this PR lands the only
  `[Unreleased]` content, the version bump + rename ship in the same merge).
- Docs: short section in the admin/metrics docs describing the master-token
  requirement and the new page.
