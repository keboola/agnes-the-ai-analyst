# Built-in marketplace + config-surface introspection

**Status:** design / spec
**Date:** 2026-06-15

## Problem

Every Agnes instance needs two kinds of in-platform guidance that today do not
ship with the product:

1. **Analyst guidance** — how to use Agnes (query rails, catalog, snapshots,
   metrics). A new analyst's Claude has only generic knowledge and no
   instance-specific rails unless an operator hand-builds a workspace.
2. **Operator guidance** — how to change the instance's content and
   configuration: the init prompt, the analyst workspace, branding /
   `instance.yaml`, connectors. This knowledge currently lives nowhere the
   operator's Claude can reach, so when an operator asks "how do I change the
   init prompt here?" the answer is guessed — and the guess is usually wrong
   (e.g. pointing at a config file that an instance renders inline, or a repo
   the live deployment no longer consumes).

The hard constraint: this repo is the **vendor-neutral OSS distribution**.
Nothing customer-specific (deployment hostnames, private repo names, project
IDs) may ship in it. Yet operator guidance is only useful if it can name *this
instance's* concrete pointers — where its workspace template lives, which
marketplaces are registered, where to change branding.

The resolution — and the core insight of this design — is that **a deployed
Agnes instance already knows most of those pointers from its own live
registered state**, so operator guidance does not need to hardcode them. It
reads them at runtime. The OSS code stays vendor-neutral; the concrete values
come from the instance's own configuration.

## Goals

- Ship a **built-in marketplace** with two plugins (`agnes-analyst`,
  `agnes-operator`) served to every instance out of the box, no admin
  registration required.
- Make the operator plugin **data-backed**: it answers using the instance's
  live configuration surface rather than hardcoded prose, so it is accurate
  per-instance and cannot drift behind the code.
- Expose that configuration surface as a **read-only introspection primitive**
  with full REST × CLI × MCP parity.
- Keep the OSS distribution **strictly vendor-neutral** — concrete pointers are
  resolved from instance state, never baked into shipped content.
- Let an admin **disable any built-in plugin** independently.

## Non-goals

- Customer-specific overlay content (e.g. a particular deployment's repo names).
  Those belong in that deployment's own registered marketplace, not here. This
  design makes such overlays *largely unnecessary* by reading live state, but
  does not build them.
- A merge layer that injects built-in skills *underneath* a custom Initial
  Workspace Template. The built-in marketplace is served via the marketplace
  channel, which is independent of the workspace payload, so the IWT-override
  problem does not apply here.
- Changing how the init prompt / IWT / `instance.yaml` mechanisms themselves
  work. This design *documents and introspects* them; it does not alter them.

## Background (verified against the codebase)

- **Config resolvers.** `app/instance_config.py` exposes the per-instance
  customization surface as `get_*` resolvers (env var > `instance.yaml` path >
  default). `docs/CONFIGURATION.md` is the authoritative human map of these,
  guarded by `tests/test_config_reference_coverage.py`.
- **Marketplace serving.** Admin-registered marketplaces (rows in
  `marketplace_registry`, each a git URL + branch + token-env) are cloned,
  parsed into `marketplace_plugins`, and served RBAC-filtered via
  `/marketplace.zip` and `/marketplace.git/*`. Served composition is
  `(admin_granted ∖ opt_outs) ∪ store_installs`; the marketplace feed has no
  god-mode shortcut (even Admin needs an explicit grant).
- **What the instance already knows about itself (introspectable):**
  - Registered marketplace repo URLs — `marketplace_registry.url`.
  - Registered Initial Workspace Template URL/branch — `instance.yaml`
    `initial_workspace.url` (read by `app/api/initial_workspace.py`).
  - All config knobs and their resolved values — the `get_*` resolvers.
- **What it does not know:** its own infrastructure/provisioning (Terraform)
  repo. The app is provisioned *by* infra, not the reverse, so there is no
  pointer. This is the single gap, closed by an optional knob (below).
- **System-seeded precedent.** `Admin` and `Everyone` groups are seeded
  `is_system=TRUE` — the pattern for "ships by default, present on every
  instance." The built-in marketplace mirrors it.

## Architecture

Three components, buildable as three phases.

### Component 1 — Config-surface introspection primitive

A read-only endpoint that returns the instance's complete configurable surface
in one call:

- **`GET /api/admin/config-surface`** (`require_admin`). Returns:
  - `knobs`: for each `get_*` resolver — `{ key, resolver, env_var, yaml_path,
    default, current_value, source }` where `source ∈ {env, yaml, default}`.
  - `initial_workspace`: `{ url, branch, last_sync_sha }` (or null if
    unregistered).
  - `marketplaces`: `[{ name, url }]` for registered marketplaces.
  - `infra_repo_url`: the value of the new knob below (empty string if unset).
- **CLI:** `agnes admin config-surface` (`--json`).
- **MCP:** a tool exposing the same payload, so an operator's Claude in
  Cowork/Claude Code can call it directly.

Computed from existing reads (`instance_config` resolvers, `marketplace_registry`,
`initial_workspace` config) — **no new table**. This is the machine-readable
form of `docs/CONFIGURATION.md`; the admin UI can later consume it too.

**New knob:** `instance.infra_repo_url` → `get_infra_repo_url()`
(`AGNES_INFRA_REPO_URL` env > `instance.infra_repo_url` yaml > `""`). Empty
default keeps OSS vendor-neutral; an operator sets it so the operator plugin can
point at the real infra repo when present. Added to `docs/CONFIGURATION.md`
(the coverage ratchet enforces this automatically).

### Component 2 — Built-in marketplace mechanism

- **Content location.** Bundled in the wheel at `src/_builtin_marketplace/`
  (offline; no network at boot), containing `.claude-plugin/marketplace.json`
  plus the two plugins.
- **Seeding.** A `marketplace_registry` row with `is_builtin=TRUE` whose source
  resolves to the bundled local path (not a git URL). Re-baked from the wheel on
  boot/upgrade; the nightly git sync skips built-in rows (nothing to fetch).
  - Schema change: new `is_builtin BOOLEAN` column on `marketplace_registry`.
  - Migration on **both** ladders (DuckDB `_vN_to_v(N+1)` in `src/db.py` +
    Alembic), matching `_pg.py` / DuckDB repo methods, and the contract test —
    per the dual-backend non-negotiables.
- **Per-plugin admin disable.** Admin can disable an individual built-in plugin
  (e.g. keep `agnes-analyst`, hide `agnes-operator`). This is **admin-level**
  state, distinct from the existing per-user `user_plugin_optouts`. Persisted as
  a disable flag keyed on `(builtin marketplace, plugin name)`; toggled through
  the existing `/admin/marketplaces` surface. Disabled built-in plugins are
  filtered out of the served feed for everyone.
- **RBAC seeding.** Seed `resource_grants`: `Everyone → agnes-analyst`,
  `Admin → agnes-operator`. (The feed has no god-mode shortcut, so the Admin
  grant must be seeded explicitly.)

### Component 3 — Content (the two plugins, vendor-neutral)

- **`agnes-analyst`** — skills covering how to use Agnes: the query/discovery
  rails, `catalog` / `schema` / `describe`, snapshots for remote tables,
  metric-definition lookup. Served to `Everyone`.
- **`agnes-operator`** — a skill covering how to configure/develop the instance:
  the layered model (init prompt + workspace via the Initial Workspace Template;
  branding/`instance.yaml`; connectors), the env > yaml > default resolution
  order and its footgun. It instructs Claude to **call the config-surface tool**
  to fill in instance-accurate pointers (registered IWT URL, registered
  marketplaces, knob values, `infra_repo_url`) rather than stating any. Names no
  deployment. Served to `Admin`.

## Data flow

```
operator asks their Claude "how do I change the init prompt here?"
   │
   ▼
agnes-operator skill (served from built-in marketplace, admin-granted)
   │  instructs: call the config-surface tool
   ▼
MCP config-surface tool → GET /api/admin/config-surface
   │  reads instance_config resolvers + marketplace_registry + initial_workspace
   ▼
answer: "your workspace template is <registered IWT url>; edit
install-prompt/template.md.tmpl there, then Sync now in /admin/server-config"
   (concrete for THIS instance, assembled from live state, zero hardcoding)
```

## Vendor-neutrality

- Shipped content (both plugins) names no deployment, host, or private repo.
  Concrete pointers are always resolved at runtime from instance state.
- The `infra_repo_url` knob defaults to empty in OSS.
- Extend the existing vendor-agnostic content scan to cover
  `src/_builtin_marketplace/` so a customer-specific token can never land there.

## Testing

- **Config-surface contract** (both backends): payload shape; `current_value` /
  `source` correctly reflect an env override vs a yaml value vs the default;
  registered IWT and marketplaces surfaced; `infra_repo_url` round-trips.
- **REST × CLI × MCP parity** for the new endpoint (the coverage gate).
- **Built-in seeding**: idempotent across reboot/upgrade; built-in row skipped
  by the git sync path; re-bake picks up wheel content.
- **Per-plugin disable**: a disabled built-in plugin disappears from the served
  feed for all callers; re-enabling restores it.
- **RBAC**: `agnes-analyst` visible to a plain user; `agnes-operator` visible
  only with the Admin grant.
- **Migration ladder**: `is_builtin` + disable-state reach the same schema
  endpoint on DuckDB and Postgres (`tests/test_db_schema_version.py`).
- **Vendor-agnostic guard** extended over the bundled content.

## Phasing

1. **Config-surface primitive** — `GET /api/admin/config-surface` + CLI + MCP +
   `instance.infra_repo_url` knob. Independently useful (machine-readable
   config map); no marketplace dependency.
2. **Built-in marketplace mechanism** — `is_builtin` column + migration,
   bundled-content seeding + re-bake, per-plugin admin disable, RBAC seed.
3. **Content** — the two plugins; the operator plugin consumes Phase 1.

Each phase is its own implementation plan / PR.

## Decisions (resolved during design)

- Built-in marketplace is a **seeded `is_builtin` row** with content bundled in
  the wheel (offline); **per-plugin** admin disable (not a single toggle).
- Introspection is a **new read-only `config-surface` endpoint** with full
  REST + CLI + MCP parity (not prose over existing endpoints, not CLI-only).
- RBAC defaults: `agnes-analyst → Everyone`, `agnes-operator → Admin`.
- The infra-repo gap is closed by an **optional `instance.infra_repo_url`
  knob** (empty default), not left untracked.
