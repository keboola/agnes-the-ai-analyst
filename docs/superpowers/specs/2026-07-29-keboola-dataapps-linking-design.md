# Linked data apps — ingest & surface externally-hosted apps (Keboola) via MCP

**Status:** design approved 2026-07-29
**Scope:** linked (externally-hosted) data apps, with Keboola-platform apps as the
first ingest source. Deliberately generic where cheap, but does NOT design the
whole "Keboola-via-MCP" integration — that is future work this pattern seeds.

## Problem

Agnes hosts its own data apps (git repo → runtime container → `/apps/<slug>/`
proxy; `repo_mode` ∈ {`internal`, `external`}). It cannot **link** to a data app
that already runs elsewhere — e.g. a Keboola-platform data app (Streamlit, its
own URL + Keboola auth). Admins want to make such apps discoverable, grant them
to users as resources, have users see them in the UI (name + description + Open),
and have the user's LLM know the description so it can recommend/point to them.

The user also wants this built on a **Keboola MCP source** (registered like the
existing CRM MCP), used both as a live passthrough tool surface AND as an
**ingest source** — data apps first, more Keboola entities later.

## Non-goals

- Re-hosting Keboola apps inside Agnes (the existing hosted path already covers
  git-sourced apps; the registry is designed to allow it later but this spec does
  not implement re-host of a linked app).
- The LLM *interacting with* / driving a linked app (awareness + description
  only; live Keboola tools remain available separately via passthrough).
- Designing ingest for non-data-app Keboola entities (buckets, flows, …). The
  projection layer is built generically so those are additive later.

## Two parts

- **Part 1 — Keboola MCP source (enabler, mostly existing).** Register the
  Keboola platform MCP as an `mcp_sources` row exactly like the CRM MCP
  (shared or per-user secret = Keboola Storage API token). Its tools are
  available as RBAC-filtered passthrough. **No new mechanism** — this is
  configuration on top of the Universal-MCP passthrough that already exists.
  This spec assumes it and does not re-specify it.
- **Part 2 — MCP-sourced linked-app resources (new design).** Use one Keboola
  MCP tool (the data-app lister) in **materialize** mode to produce a
  `keboola_data_apps` table, then **project** that table into the `data_apps`
  registry as `linked` rows that are grantable resources, visible in the UI/CLI,
  and known to the LLM.

## Architecture

```
Keboola platform MCP (mcp_sources row, Part 1)
        │  tool "list data apps"  (tool_registry mode = materialize)
        ▼
connectors/mcp extractor  ──►  extract.duckdb: keboola_data_apps table
   (existing Universal-MCP materialize path)     (keboola_app_id, name,
        │                                          description, url,
        │  SyncOrchestrator rebuild                project/branch, updated_at)
        ▼
Projection reconciler (NEW, generic seam)
   read keboola_data_apps  ──►  upsert data_apps rows (repo_mode="linked")
   key = (source_ref, keboola_app_id); soft-delete on disappearance
        ▼
data_apps registry  ──►  ResourceType.DATA_APP grants (EXISTING)
        ├─► REST  GET /api/data-apps (RBAC-filtered)   [existing]
        ├─► Web   /apps table + /apps/detail/{slug}    [extend: kind conditionals]
        ├─► CLI   agnes app list                       [extend: linked badge]
        └─► MCP   data_apps_list / data_app_get        [existing; linked appears]
```

### The "linked" kind is generic; Keboola is the first adapter

The projection reconciler maps a materialized MCP-entity table → `data_apps`
rows. The Keboola-specific bit is confined to a small **ingest adapter** (which
materialized table + column mapping + which `source_ref`). A future entity type
reuses the same shape `{materialize tool → projection adapter → resource upsert}`
against a different resource type.

## Data model — `data_apps` registry additions

New/changed columns on `data_apps` (DuckDB `_vN_to_v(N+1)` **and** Alembic step,
both backends, contract test):

| Column | Type | Meaning |
|---|---|---|
| `repo_mode` | existing | now also accepts `"linked"` (∪ `internal`/`external`) |
| `external_url` | TEXT, nullable | deployment URL of the externally-hosted app (linked only) |
| `source_ref` | TEXT, nullable | provenance: `<connection_id>:<keboola_app_id>` (mirrors #1096 per-connection `source_ref`); NULL for hosted apps |
| `managed` | BOOLEAN, default FALSE | TRUE ⇒ row is sync-owned; admin may only override `description` |
| `description_override` | TEXT, nullable | admin-set description that the sync must not clobber (managed rows) |

Invariants for `repo_mode="linked"`:
- No container is ever spawned; `state` is a fixed `"linked"` (not part of the
  deploy lifecycle). No deploy / stop / logs / preview-grant / git-credential.
- `external_url` is required; `repo_url`/`repo_branch` are empty.
- `owner_user_id` = a system/admin sentinel (linked apps are org resources, not
  personally-owned).
- Effective description = `description_override` if set, else synced `description`.

`ResourceType.DATA_APP` grants (`resource_grants`, resource_id = slug) and the
`/admin/access` grant-picker are unchanged — linked slugs are grantable exactly
like hosted ones. `_data_app_blocks()` (the grant-picker projection) includes
linked rows.

## Ingest & projection

- **Materialize** (existing): the admin sets the Keboola MCP data-app lister tool
  to `materialize` in `tool_registry`. The Universal-MCP extractor writes
  `keboola_data_apps` into that source's `extract.duckdb`; the SyncOrchestrator
  ATTACHes it as a queryable view (bonus LLM surface — the agent can `query` the
  full project catalog, independent of grants-to-apps).
- **Projection reconciler** (`src/data_apps/linked_projection.py`, new): after a
  source sync, for each `keboola_data_apps` row upsert a `data_apps` row keyed by
  `(source_ref, keboola_app_id)`:
  - insert → new `linked` row, `managed=TRUE`, slug derived from Keboola app id
    (stable, collision-safe), `description_override` untouched.
  - update → refresh `name`, `external_url`, synced `description`; **never**
    touch `description_override` or grants.
  - disappearance → soft-delete (mark inactive / `state` hidden), keep the row +
    grants so a re-appearing app re-links losslessly; `log()` what was hidden
    (no silent drop).
  - scoped **per `source_ref` connection** — one connection's reconcile never
    hides another connection's linked apps (mirrors #1096 prune-scoping).
- **Ingest adapter** (`src/data_apps/keboola_adapter.py`, new, thin): the only
  Keboola-aware code — declares the materialized table name + the column mapping
  (`keboola_app_id/name/description/url` ← actual materialized columns) and the
  `source_ref` scheme. If the MCP tool returns only partial metadata (no URL or
  description), the adapter documents a Storage API supplement as a follow-up;
  the projection contract (needs id+name+url) is the seam, not the transport.

## Surfaces (REST × CLI × MCP × Web — all four)

| Operation | REST | CLI | MCP | Web UI |
|---|---|---|---|---|
| Register Keboola MCP source (Part 1) | `POST/PUT /api/admin/mcp-sources` ✅ | `agnes admin mcp source …` ✅ | `admin_source_connections_list` ✅ | `/admin` MCP sources ✅ |
| Set lister tool → materialize | tool_registry API ✅ | `agnes admin mcp tool …` ✅ | — | `/admin` tool grants ✅ |
| Trigger/inspect sync+projection | `POST /api/sync/trigger`, `GET /api/jobs` ✅ | `agnes admin sync/jobs` ✅ | `admin_jobs_list` ✅ | `/admin` jobs ✅ |
| List apps incl. linked (filter) | `GET /api/data-apps?kind=linked` ✳️ | `agnes app list [--linked]` ✳️ | `data_apps_list` ✅ (linked shown) | `/apps` table ✳️ |
| Grant linked app to group/user | `resource_grants` API ✅ | `agnes admin grant data_app <slug> <grp>` ✅ | grant tool ✅ | `/admin/access` ✅ |
| Override description (managed) | `PATCH /api/data-apps/{slug}` ✳️ NEW | `agnes app set-description <slug>` ✳️ NEW | `data_app_set_description` ✳️ NEW | edit control on detail ✳️ |
| See my apps / open | `GET /api/data-apps` ✅ | `agnes app list` ✅ | `data_apps_list` ✅ | `/apps` Open ↗ ✳️ |

✅ existing · ✳️ new/extended. The **only genuinely new endpoints** are the
`kind` filter and the description-override op — the latter added across all four
surfaces to satisfy the coverage ratchet (REST × CLI × MCP) + web.

### Web UI (fits the existing `/apps`)

Linked apps are **rows in the same `data_apps.html` table**, distinguished by
`repo_mode`/kind — not a separate page:
- **State** column shows a `linked` badge.
- **Owner** column shows the Keboola source (project/connection) for linked rows.
- **Open ↗** links directly to `external_url` (new tab), always enabled (no
  `running`-state gate — that gate is hosted-only).
- **Detail page** (`data_app_detail.html`): for linked rows show description +
  source + Open, and (admin) a **description-override** editor; **hide** the
  Deploy/Stop/logs controls (hosted-only, already `can_manage`-gated — add a
  `kind == hosted` guard).
- Hero subtitle broadens: "Hosted web apps deployed from a git repo, plus linked
  apps running elsewhere (e.g. Keboola)."
- Opening a linked app relies on the **external platform's own auth** (Keboola
  SSO / app password). Agnes does not proxy or issue cookies for linked apps.

### LLM context / skill / knowledge (RBAC-scoped to the caller's grants)

- **Phase 1 (must):** `data_apps_list` / `data_app_get` already RBAC-filter and
  carry `name + description + url`; linked apps simply appear. Zero new plumbing
  — the chat agent and local stdio `agnes mcp` (post-#1102) both see "your
  available apps" with descriptions + URLs. This is the primary awareness channel.
- **Phase 2 (designed, deferrable):** project a corporate-memory **knowledge
  item** per linked app ("<name> — <description>; open at <url>"), **scoped to
  the same resource grants** as the app, so `knowledge_search` surfaces it on a
  semantic query ("which app shows X?"). Visibility MUST equal the app's grants
  (no leak). Deferred behind a flag; Phase 1 satisfies the stated requirement.
- No per-app skill for now (YAGNI); revisit once several linked apps exist.

## Backend parity, migration, guards

- `src/repositories/data_apps.py` (DuckDB) **and** `data_apps_pg.py` (PG) get the
  same new methods (linked create, projection upsert, soft-delete, description
  override) in the same change; `tests/db_pg/test_*_contract.py` extended.
- Migration: DuckDB `_vN_to_v(N+1)` + Alembic step reach the same schema;
  `tests/test_db_schema_version.py` gate.
- New REST endpoints gated with the correct `require_*` dependency; new
  `ResourceType` is NOT needed (reuse `DATA_APP`).
- Triple-surface: the description-override endpoint gets a CLI command + an MCP
  tool + a `tests/test_documentation_api_triple_surface.py` classification; the
  `kind` filter is a param on an existing endpoint (no new triple-surface row).
- Command-UX: `agnes app list` keeps its shape; `--linked` is a filter, not a new
  scope flag; "not found" hints unchanged.

## Testing

- Projection reconciler unit tests: insert/update/soft-delete/re-appear;
  `description_override` preserved across sync; per-`source_ref` isolation
  (connection A's reconcile leaves connection B's linked rows).
- Repo parity contract tests (DuckDB ↔ PG) for the new methods.
- Endpoint RBAC tests: viewer sees granted linked apps only; description-override
  is admin/owner-gated; `kind` filter.
- Web render test: `/apps` shows a linked row with Open → external_url; detail
  hides deploy controls for linked.
- Ingest adapter tested against a **mocked** materialized `keboola_data_apps`
  table (no live Keboola MCP needed for CI). Live E2E against a real Keboola MCP
  source is a follow-up once one is registered on an instance.

## Rollout / flags

- Reuses the `data_apps` feature gate; linked rows are inert until a Keboola MCP
  source + materialize tool + a grant exist. No new global flag required (the
  Phase-2 knowledge projection sits behind its own sub-flag).

## Open dependency (verify in planning)

The Keboola platform MCP must expose a tool that lists data-app configs with at
least `{id, name, url}` (description preferred). If it returns only `{id, name}`,
the ingest adapter supplements URL/description via the Keboola Storage API
(`keboola.data-apps` component configs) — confined to the adapter, no change to
the projection/registry contract.
