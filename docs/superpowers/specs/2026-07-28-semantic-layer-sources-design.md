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
    failure into an at-save validation.
  - `GET` connection responses gain `has_master_secret: bool` (alongside the
    existing secret-status fields from `_with_secret_status`).
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
  - DuckDB: `_v105_to_v106` step in `src/db.py` (+ snapshot DDL update).
  - PG: matching Alembic revision. Both ladders reach the same endpoint;
    `tests/test_db_schema_version.py` gates it.
- Semantics: for rows with `source='keboola_semantic_layer'`, `source_ref` is the
  originating `source_connections.id`. For `manual` / `yaml_import` rows it stays
  NULL and is ignored.
- **Legacy-row adoption:** pre-existing semantic rows have `source_ref IS NULL`.
  The sync pass for the **default** Keboola connection treats NULL-ref rows as its
  own: upserts stamp `source_ref`, and its prune scope is
  `(source='keboola_semantic_layer' AND (source_ref = :id OR source_ref IS NULL))`.
  Non-default connections' prune scope is strictly `source_ref = :id`. Result: an
  upgrade produces no duplicates and no cross-source deletion.
- Repository changes (`metrics.py`/`metrics_pg.py`, `glossary.py`/`glossary_pg.py`)
  land in the same PR for **both** backends, with the cross-engine contract tests
  extended to cover `source_ref` round-tripping and scoped listing.

### 3. Multi-source sync loop

`sync_semantic_layer()` becomes an orchestrating loop; the existing single-project
logic is extracted into a per-source function taking
`(stack_url, token, source_ref)`.

- **Source enumeration:** all `source_type='keboola'` connections that have a
  `{id}:master` vault secret, default connection first.
- **Backward-compat fallback:** if **no** connection has a master token, run
  today's single-source resolution (`_resolve_keboola_credentials`: explicit args →
  legacy `KEBOOLA_STACK_URL`/`KEBOOLA_STORAGE_TOKEN` full pair → default
  connection's regular token), with `source_ref` = default connection id when the
  credentials came from a connection, else NULL. In this mode the prune scope is
  **all** `source='keboola_semantic_layer'` rows regardless of `source_ref`
  (single-source semantics, same as today) — this also cleans up correctly when an
  admin removes the last master token after multi-source rows were stamped.
  Behavior for existing installations is unchanged, including the master-token
  preflight error message.
- **Precedence note:** when at least one master token exists, master-token sources
  are the *only* sources — the legacy env pair is not additionally synced (it has
  no identity to scope a prune to; mixing it in would recreate the cross-wipe
  problem).
- **Name-conflict policy:** metric names must stay unique for catalog UX. If an
  incoming metric's name already exists with a different `source_ref` (or a
  non-semantic `source`), the row is **skipped**, counted as `skipped_conflict`,
  and reported per source. Ownership is sticky (the source whose ref is already on
  the row keeps it), so sync order does not decide outcomes. Same policy for
  glossary terms.
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
  (`tests/test_design_system_contract.py`) apply.
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
- `/admin/data-sources` keeps only a one-line semantic-layer status with a link to
  the new page (replacing the current multi-line summary block).
- Navigation: the page is linked from the admin nav the same way sibling admin
  pages are registered.

### 5. Testing

- **Resolution/loop:** multi-source enumeration; fallback path when no master
  token; legacy pair not synced alongside master sources.
- **Prune isolation:** two sources, deleting upstream metrics from one never
  touches the other's rows; NULL-ref adoption by the default connection.
- **Conflicts:** cross-source name collision → skip + counted, sticky ownership.
- **Endpoint:** `kind` validation (non-keboola 400, bad value 422/400), master
  verification on save (non-master 400, Storage API outage → structured error),
  `has_master_secret` exposure.
- **Migration:** both ladders reach v106 (`tests/test_db_schema_version.py`),
  contract tests cover `source_ref` on both backends.
- **Page:** route smoke test (auth, renders, sources listed), design-system
  contract suite, screenshot during implementation.
- Full suite before every push (`.venv/bin/pytest tests/ --tb=short -n auto -q`).

## Rollout / compatibility

- No config file changes; no new env vars. Existing single-project installs see
  identical behavior until a master token is saved.
- CHANGELOG bullets under `[Unreleased]` (Added: master token + page + multi-source;
  Changed: data-sources summary block) in the same PR.
- Docs: short section in the admin/metrics docs describing the master-token
  requirement and the new page.
