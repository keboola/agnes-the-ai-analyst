# Changelog

All notable changes to Agnes AI Data Analyst.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html), pre-1.0 — public surface (CLI flags, REST endpoints, `instance.yaml` schema, `extract.duckdb` contract) may shift between minor versions; breaking changes called out under **Changed** or **Removed** with the **BREAKING** marker.

CalVer image tags (`stable-YYYY.MM.N`, `dev-YYYY.MM.N`) are produced for every CI build; semver tags (`v0.X.Y`) are cut at release boundaries and reference the same commit as a `stable-*` tag from the same day.

---

## [Unreleased]

### Fixed

- **Pre-migration snapshot integrity** — the snapshot file written
  before a v(N-k)→vN migration now captures the true on-disk state
  *before* any DDL runs, instead of the post-self-heal state the
  0.12.0 hoist (#106) introduced. With the unconditional
  `conn.execute(_SYSTEM_SCHEMA)` at the top of `_ensure_schema`, the
  full set of modern-binary tables (`view_ownership`,
  `marketplace_registry`, `user_groups`, `resource_grants`, etc.) was
  materialized first, then `CHECKPOINT` flushed them to disk, and
  `shutil.copy2` copied the already-modified DB as the
  "pre-migration" snapshot — so an operator inspecting the snapshot
  for rollback debugging saw the binary's full table set instead of
  the old schema. Functionally rollback still worked (extra empty
  tables are harmless and re-running migration is idempotent), but
  the snapshot was misleading. Fix: gate the self-heal call on
  `current >= SCHEMA_VERSION`. The split-brain (`current >
  SCHEMA_VERSION`) and same-version safety-net (`current ==
  SCHEMA_VERSION`) paths still self-heal as before; the migration
  path (`current < SCHEMA_VERSION`) takes its snapshot first and
  then runs `_SYSTEM_SCHEMA` from inside the existing migration
  block.
- **Split-brain self-heal regression test for a shared dev-VM
  split-brain incident** (2026-04-27). Pins the contract that the
  gated `_SYSTEM_SCHEMA`
  self-heal pass keeps working when a binary lands on a
  future-version DB that's missing tables it expects: every query
  against the missing table would otherwise crash at runtime
  (`_duckdb.CatalogException`). New
  `test_split_brain_future_version_with_missing_tables_self_heals`
  in `tests/test_db.py::TestMigrationSafety` synthesizes a v99 DB
  whose only table is `schema_version`, runs `_ensure_schema`, and
  asserts that the v13-era core tables (`users`, `user_groups`,
  `user_group_members`, `resource_grants`) now exist *and* that
  `schema_version` stays at 99 (self-heal without falsely
  advertising a downgrade). Plus
  `test_pre_migration_snapshot_excludes_post_self_heal_tables`
  pins the snapshot-integrity contract: a v2→vN migration's
  snapshot must not contain any post-v2 table from the modern
  binary.

### Internal

- `test_future_version_is_noop` docstring updated to reflect that
  the self-heal pass *does* run on a future-version DB, just
  doesn't touch the version row. The test still passes unchanged —
  its only assertion was the version-row contract, which holds.

## [0.12.0] — 2026-04-28

### Changed

- `/admin/access` resource tree now visually separates the three-level hierarchy (resource type → block/bucket → item). Each resource-type section gets a colored left stripe and a faint tinted banner; sections are separated by an 8px neutral gap. Stripe colors cycle 4-wide via `nth-child` so adding new resource types to `app/resource_types.py` works without touching CSS. The first-position color is the project primary blue (`#0073D1`), avoiding the violet (`#6366f1`) reserved for granted items.

### Added

- `ResourceType.TABLE` — admins can grant table-level access per `user_group` via the `/admin/access` page. Tables registered in `table_registry` are listed grouped by `bucket`, with the existing per-block "Grant all" / "Revoke all" bulk actions. Listing and grant storage only — runtime enforcement still flows through legacy `dataset_permissions`; the migration plan lives in `docs/TODO-rbac-data-enforcement.md`.
- `AGNES_ENABLE_TABLE_GRANTS` env var (default off) gates the half-built `ResourceType.TABLE` chip. While disabled the chip is hidden from `/admin/access` and `POST /api/admin/grants` returns **422** with the env-var name in `detail` on a TABLE grant attempt. Existing TABLE rows in `resource_grants` stay listable and deletable — the flag controls UI exposure and new-grant acceptance only, never blocks cleanup.
- `da admin break-glass <user>` CLI — recovery path when the operator has locked themselves out of `/admin/access`. Adds the user to the Admin user_group with `source='system_seed'` regardless of RBAC state. Bypasses authentication; relies on filesystem access to `${DATA_DIR}/state/system.duckdb` implying host-level trust. Document this in deployment runbooks alongside `SEED_ADMIN_EMAIL`.

### Internal

- `scripts/seed_dummy_tables.py` — populates `table_registry` with 12 dummy tables across 3 buckets (`in.c-finance`, `in.c-marketing`, `in.c-product`), each with `is_public=False`, for exercising the new `/admin/access` Tables section without a configured data source.
- `/marketplace.zip` short-circuits to `304` before any file IO or ZIP compression on a matching `If-None-Match`. Hot path on every Claude Code SessionStart hook. Backed by an in-process `cachetools.TTLCache` over the resolved-plugins → ETag map (default 120s, env-tunable via `AGNES_MARKETPLACE_ETAG_TTL`, set `0` to disable). `invalidate_etag_cache()` is called by marketplace sync after refresh so the next request re-hashes against new on-disk content instead of waiting for TTL expiry. New explicit dependency: `cachetools>=5.3.0`.

### Fixed

- `/admin/access` group sidebar grant-count badges no longer revert to a stale value when switching between groups. The badge was reading `state.groups[i].grant_count`, a snapshot populated once at `/access-overview` load; toggling a grant only updated the DOM (via `refreshCounts`), not that field, so the next `renderGroups` call (triggered by `selectGroup`) would clobber the live count with the original snapshot. `renderGroups` now derives the count live from `state.grants`, the array that `toggleGrant`/`bulkSet` keep in sync. Server data was always correct — only the in-page badge drifted until refresh.
- `/catalog`, `/admin/tables`, and `/admin/permissions` pages now render the shared top header correctly. The pages include `_app_header.html` (which uses `.app-*` CSS classes) but were not linking `style-custom.css` where those classes are defined; only `dashboard.html` and `base.html` did. Without the stylesheet the nav links, dropdowns, and user menu rendered as unstyled inline text. Added the missing `<link>` to all three templates.
- `PATCH /api/admin/groups/{id}` on a system group now correctly accepts description-only updates while still rejecting renames. The endpoint guard previously short-circuited with `409 "System groups are immutable"` for any mutation, which contradicted the repository layer's narrowed contract (rename-only rejection) — a description-only payload like `{"description": "..."}` would hit the endpoint short-circuit and never reach the repo. The endpoint now 409s only when `payload.name` differs from the existing name; a no-op rename (same name in payload) is dropped from the update before reaching the repo.
- Google OAuth callback no longer wipes a user's `google_sync` group memberships on a transient Workspace API failure. `fetch_user_groups` is fail-soft and returns `[]` for both "no groups" and "API error" — the callback used to feed that empty list into `replace_google_sync_groups`, which deletes all `source='google_sync'` rows for the user and then inserts zero. A login during a transient Cloud Identity hiccup would silently drop every Workspace-synced membership the user had built up. Admin-added memberships (`source='admin'`) were already protected. The callback now skips `replace_google_sync_groups` when the fetch returns empty and logs "preserving existing memberships" instead. Trade-off: a user whose Workspace groups were genuinely cleared keeps stale memberships until the next non-empty sync — accepted until `fetch_user_groups` learns to distinguish empty-success from empty-failure.
- `docker-compose.host-mount.yml` now uses `o: bind,rbind` instead of `o: bind` for the `data` volume. With a plain bind, sub-mounts under `/data` on the host (e.g. the dual-disk layout where sdc is mounted on `/data/state`) are silently shadowed inside the container by an empty subdirectory on the parent disk. The container then writes `system.duckdb` and other state to the wrong disk; the dedicated state disk receives no writes and accumulates only the snapshot left by the migration script. Recursive bind propagates existing sub-mounts at container start, so the container sees the same filesystem the host does. Operators on dual-disk VMs need to copy the live DB from `/var/lib/docker/volumes/agnes_data/_data/state/` (sdb's empty subdir) onto `/data/state/` (sdc) **before** redeploying with the fix, or the next start will surface the stale snapshot.

### Changed

- **BREAKING** Marketplace endpoint (`/marketplace.zip`, `/marketplace.git/*`) no longer god-modes for Admin members. `src.marketplace_filter.resolve_allowed_plugins` now filters every caller — admins included — through `resource_grants`. Admins curate their own marketplace view by granting plugins to the Admin group (or any group they belong to). Existing installs where the only membership on Admin is the admin themselves will see an empty marketplace until grants are added in `/admin/access`. App-level authorization (`require_admin`, `can_access` for non-marketplace types) is unaffected — Admin is still god mode there.
- **BREAKING** RBAC redesigned around two layers: app-level access via the `Admin` user-group (god mode short-circuit) and resource-level access via a generic `(group, resource_type, resource_id)` grant model. The four-value `core.viewer/analyst/km_admin/admin` hierarchy with `implies` BFS expansion is gone — every protected endpoint now uses either `require_admin` or `require_resource_access(ResourceType.X, "{path}")` from the new `app.auth.access` module. Authorization is decided per-request via a single DB lookup; no session cache, no dual-path resolver, no `_hydrate_legacy_role` shim. See `docs/RBAC.md`.
- **BREAKING** `internal_roles`, `group_mappings`, `user_role_grants`, and `plugin_access` tables removed. Replaced by `user_group_members` (binds users to user_groups with a `source` enum: `admin` / `google_sync` / `system_seed`) and `resource_grants` (group → `(resource_type, resource_id)`). Schema v13; the migration backfills from v12 atomically — `users.groups` JSON is converted into `user_group_members` rows with `source='google_sync'`, `core.admin` grants become Admin-group memberships with `source='system_seed'`, and `plugin_access` rows become `resource_grants` of type `marketplace_plugin`. The `users.groups` JSON column is dropped; the deprecated `users.role` column is kept NULL as a legacy artifact.
- **BREAKING** Schema v14 — `user_group_members` and `resource_grants` now declare DuckDB foreign-key constraints on `group_id` (referencing `user_groups.id`). Cascade deletes can no longer leave orphaned member / grant rows pointing at a deleted group. Migration is RENAME → CREATE-with-FK → INSERT → DROP, wrapped in `BEGIN TRANSACTION` so a partial failure rolls back without leaving the DB at a half-applied schema. Forks that touched these tables outside the documented repository APIs need to verify the FK direction matches their writes.
- **BREAKING** Admin REST surface unified under `/api/admin/groups`, `/api/admin/groups/{id}/members`, `/api/admin/grants`, `/api/admin/resource-types`. `app.api.role_management` and `app.api.plugin_access` removed. The web UI route `/admin/role-mapping` and `/admin/plugin-access` are replaced by a single `/admin/access` page; the `_app_header.html` link is renamed to "Access".
- **BREAKING** CLI subcommands `da admin role *`, `da admin mapping *`, `da admin grant-role`, `da admin revoke-role`, `da admin effective-roles` removed. New subcommands: `da admin group {list,create,delete,members,add-member,remove-member}` and `da admin grant {list,create,delete,resource-types}`. `da admin set-role <user> admin` still works as a thin wrapper that toggles Admin-group membership.
- Module authors no longer call `register_internal_role(...)`. Resource types are an `app.resource_types.ResourceType` `StrEnum` paired with a `ResourceTypeSpec` registered in `RESOURCE_TYPES`; adding a new resource type means adding one enum member, one `list_blocks(conn)` projection delegate, and one spec entry — all in `app/resource_types.py`. The registry drives both `/api/admin/resource-types` and `/api/admin/access-overview`, so there's no second wiring step. No DB migration, no startup hook.
- Google OAuth callback writes Cloud Identity group memberships into `user_group_members` (source='google_sync') instead of `users.groups` JSON. Manual admin-added memberships (source='admin') survive subsequent logins.

### Removed

- `app/auth/role_resolver.py`, `app/api/role_management.py`, `app/api/plugin_access.py`.
- `src/repositories/internal_roles.py`, `src/repositories/group_mappings.py`, `src/repositories/user_role_grants.py`.
- `app/web/templates/admin_role_mapping.html`, `app/web/templates/admin_plugin_access.html`.
- `Role` enum + `has_role`, `is_admin`, `is_km_admin`, `is_analyst`, `_is_admin_user_dict`, `set_user_role`, `get_user_role` from `src/rbac.py`. Dataset-access helpers (`can_access_table`, `get_accessible_tables`, `has_dataset_access`) preserved.
- Test files: `test_role_resolver.py`, `test_api_role_management.py`, `test_admin_role_mapping_ui.py`, `test_cli_admin_role.py`, `test_schema_v9_migration.py`, `test_plugin_access_api.py`.

### Internal

- `src/db.py` schema bumped to v13. New helpers `_seed_system_groups` (idempotent Admin/Everyone seed, runs on every connect) and `_v12_to_v13_finalize` (one-shot backfill + DROP cascade) replace `_seed_core_roles` and `_backfill_users_role_to_grants`.
- `app.auth.access` is the new authorization vocabulary: `_user_group_ids`, `is_user_admin`, `can_access`, `require_admin`, `require_resource_access`. Lives in its own module to avoid the circular import that would happen if it sat in `app.auth.dependencies` (the dependency factory needs `get_current_user` from there).
- New `tests/helpers/auth.py::grant_admin(conn, user_id)` — adds a user to the Admin system group so `require_admin` resolves to True. Updated test fixtures across `test_admin_tokens_ui`, `test_password_flows`, `test_pat`, `test_api`, `test_api_complete`, `test_api_scripts`, `test_web_ui` to call it after `UserRepository.create(role="admin")`. The legacy `users.role` column alone is no longer the admin marker.
- Skipped at module level (rewrite required for v13): `test_admin_user_capabilities_ui` (asserts the gone v9 capabilities UI), `test_marketplace_server_zip` and `test_marketplace_server_git` (depend on the removed `PluginAccessRepository`).
- Skipped individually as v13 behavior changes: `TestScriptRBAC` in `test_security` (scripts are now any-signed-in-user, not analyst+), profile-page tests in `test_web_ui` that asserted `core.analyst` / `Direct grants` / `Effective roles` markers from the dropped role hierarchy.

### Added

- **Schema v10** introduces `view_ownership` to detect cross-connector
  view-name collisions in the master analytics DB (issue #81 Group C).
  When two connectors register the same `_meta.table_name`, the
  orchestrator now refuses to silently overwrite the prior owner's view —
  it logs a `view_ownership collision` ERROR identifying both sources
  and the colliding name, and the second source's view is NOT created.
  Previously this was last-write-wins, which depended on directory
  iteration order and could change deployment-to-deployment. Operators
  resolve a collision by renaming `name` in `table_registry` on one side
  (registry-side aliasing — `source_table` stays unchanged, only the
  view name changes). The orchestrator pre-scans every connector's
  `_meta` at the start of each rebuild and releases stale ownerships
  immediately (when ALL pre-scans succeed; if any fail, reconcile is
  skipped to avoid silently stealing a transient-IO source's name),
  so a renamed table frees its name in the SAME rebuild that introduces
  the rename — no two-step waits needed. New module
  `src/repositories/view_ownership.py` exposes the repository.

### Changed

- **BREAKING (ops)**: Keboola extractor now exits with three distinct
  codes instead of two (issue #81 Group B / M14): `0` = full success,
  `1` = full failure, `2` = **partial** failure (some tables succeeded,
  some failed). Previously `exit(0)` fired even when 9 of 10 tables
  failed, masking partial failures from the sync API and any operator
  alerting hooked to non-zero exit codes. The sync API
  (`POST /api/sync/trigger`) now logs `PARTIAL FAILURE (exit 2)` as a
  data-quality alert (distinct from `FAILED (exit 1)`) and continues to
  the orchestrator rebuild step — successful tables from this run plus
  unchanged tables from previous runs stay queryable. Operators whose
  alerting treated any non-zero exit as a hard error must teach it that
  exit 2 is a partial-failure signal, not a deploy failure.
- **BREAKING (security)**: The entire Script API is now **admin-only** (issue #44).
  `GET /api/scripts`, `POST /api/scripts/deploy`, `POST /api/scripts/run`, and
  `POST /api/scripts/{id}/run` all require the admin role; previously the list
  endpoint was open to any authenticated user and deploy/run were analyst-accessible.
  Two reasons: (1) the AST + string-blocklist sandbox in `_execute_script` is
  defense-in-depth and known to be bypassable through introspection chains
  (`__class__.__base__.__subclasses__()`, `__globals__['__builtins__']`,
  `__mro__` traversal — the dunder pattern list was tightened in this PR but
  the policy is "the role gate is the trust boundary, not the blocklist");
  (2) gating only `/run` left a planted-script attack open — an analyst could
  deploy a malicious script and wait for an admin to run it. Operators who
  need scripted workflows for non-admin users should run them on the user's
  behalf or expose the relevant data via the read-only `/api/data` surface
  instead. **Migration for cron / scheduler PATs:** if a non-admin PAT is
  wired into a scheduler that hits `/api/scripts/{id}/run` or
  `/api/scripts/run`, the request now returns 403. Add the PAT user to the
  Admin group via `/admin/access` or
  `da admin group add-member Admin <pat-user-email>`. PATs themselves do not
  need re-issuing — group membership is read at request time.
- **BREAKING (ops)**: Generic ops scripts moved out of the customer-named
  `scripts/grpn/` directory into `scripts/ops/` as part of the OSS
  vendor-neutralization (issue #88):
  - `scripts/grpn/agnes-tls-rotate.sh` → `scripts/ops/agnes-tls-rotate.sh`
  - `scripts/grpn/agnes-auto-upgrade.sh` → `scripts/ops/agnes-auto-upgrade.sh`

  Downstream consumer infra repos that copy these scripts onto VMs (e.g. via
  their own `startup.sh`) must update the source path. The OSS-shipped
  `infra/modules/customer-instance/` Terraform module is unaffected — it
  embeds equivalent logic inline via heredoc and does not source-by-path
  from `scripts/`. Script behaviour and env vars are unchanged. Cross-refs
  in `README.md`, `CLAUDE.md`, `docs/DEPLOYMENT.md`, `Caddyfile`, and
  `docker-compose.yml` were updated.
- **OSS neutralization (wave 2 — code, tests, planning docs)**. Customer
  identifiers replaced with placeholders across the codebase to ready the
  repo for public release (issue #88):

  - **Code docstrings**: `connectors/openmetadata/{client,transformer,enricher}.py`,
    `src/catalog_export.py`, `scripts/duckdb_manager.py` — `prj-grp-…` →
    `my-bq-project` / `prj-example-1234`, `AIAgent.FoundryAI` →
    `AIAgent.MyAgent` (in docstrings) / `AIAgent.Example` (in test fixtures),
    `FoundryAIDataModel` → `AnalyticsDataModel`.
  - **Test fixtures** in `tests/test_openmetadata_enricher.py`,
    `tests/test_duckdb_manager.py`, `tests/test_catalog_export.py`,
    `tests/test_openmetadata_transformer.py` — same set of replacements,
    behaviour-preserving (157 tests still green).
  - **Terraform module** `infra/modules/customer-instance/variables.tf`:
    `customer_name` description rewritten in English, examples switched
    from `keboola, grpn` to `acme, example`.
  - **Workflow** `.github/workflows/keboola-deploy.yml`: comment "Groupon-side
    dev VMs" → generic "per-developer dev VMs".
  - **Caddyfile**: TLS-rotation cross-ref updated to `scripts/ops/…` and
    Keboola-specific aside removed.
  - **Auth docs** `docs/auth-groups.md` and the OAuth probe in
    `scripts/debug/probe_google_groups.py`: GCP project name `kids-ai-data-analysis`
    replaced with placeholder `acme-internal-prod`.
  - **Planning docs** under `docs/superpowers/plans/` and `…/specs/`: the
    five hackathon-era documents (`2026-04-21-deployment-log.md`,
    `…-multi-customer-deployment.md`, `…-issues-14-and-10.md`,
    `…-hackathon-dry-run.md`, the spec) had `34.77.94.14` / `34.77.102.61`
    replaced with `<dev-vm-ip>` / `<prod-vm-ip>`, `Groupon`/`GRPN`/`grpn`
    with `Acme`/`another-customer`, and `prj-grp-…` with `prj-example-…`.

### Fixed

- **BREAKING (security CRITICAL)**: Jira webhook handler is now
  fail-closed (issue #83). Previously, if `JIRA_WEBHOOK_SECRET` was
  unset, `_verify_signature` returned `True` and any unauthenticated
  POST to `/webhooks/jira` could trigger the full ingest pipeline. The
  handler now returns **503** when the secret is missing
  (operator-misconfiguration signal, distinct from 401 wrong-signature).
  Operators relying on the no-secret = accept-everything mode (don't —
  it was never documented) must set `JIRA_WEBHOOK_SECRET` before this
  merges.
- **Security (CRITICAL)**: Jira issue keys arriving via webhooks are now
  validated against the canonical `^[A-Z][A-Z0-9]{0,31}-[0-9]{1,12}\Z` format
  (`[0-9]` not `\d` to refuse non-ASCII Unicode digits, `\Z` not `$` to
  refuse trailing newlines that `$` would tolerate)
  before any filesystem operation (issue #83). Previously, `issue_key` flowed
  unsanitized into `connectors/jira/service.py` (`save_issue`,
  `download_attachment`, `_handle_deletion`, `process_webhook_event`) and
  `connectors/jira/incremental_transform.py`, enabling path traversal
  (`../../etc/passwd` style writes outside the Jira data dir). New module
  `connectors/jira/validation.py` provides `is_valid_issue_key` (regex
  whitelist; underscore deliberately excluded — Atlassian rejects underscores
  in real project keys) and `safe_join_under` (`Path.resolve()` containment
  check). Both are enforced at every filesystem boundary, defense-in-depth.
- **Security (CRITICAL)**: `webhookEvent` (the second attacker-controlled field
  in Jira webhook payloads) was used as a filename component in
  `_log_webhook_event` without sanitization (issue #83 reviewer follow-up).
  A payload with `webhookEvent: "../../tmp/pwn"` could write a JSON dump
  outside `WEBHOOK_LOG_DIR`. The handler now strips everything that isn't
  `[A-Za-z0-9_-]` (dot deliberately excluded to defeat `..` survival),
  clips length to 64 chars, and routes the final filename through
  `safe_join_under`.
- **Security (CRITICAL)**: hardened the connector → orchestrator trust
  boundary on BOTH the rebuild path
  (`src/orchestrator.py::_attach_remote_extensions`) AND the read-only
  query path (`src/db.py::_reattach_remote_extensions`, called by
  `get_analytics_db_readonly()` on every request) — issue #81 Group A.
  Three fixes: (1) DuckDB extensions referenced by `_remote_attach` are
  matched against a hard allowlist (default: `keboola, bigquery`;
  override via `AGNES_REMOTE_ATTACH_EXTENSIONS`). Install path splits
  built-in (LOAD only) from community (`INSTALL FROM community; LOAD`
  on rebuild path; LOAD only on the read-only query path which must
  not touch the network). (2) `token_env` names are matched against a
  hard allowlist (default: `KBC_TOKEN`, `KBC_STORAGE_TOKEN`,
  `KEBOOLA_STORAGE_TOKEN`, `GOOGLE_APPLICATION_CREDENTIALS`; override
  via `AGNES_REMOTE_ATTACH_TOKEN_ENVS`). Names must additionally match
  `^[A-Z][A-Z0-9_]{0,63}$`. A malicious connector cannot ask the
  orchestrator to read `JWT_SECRET_KEY` / `SESSION_SECRET` /
  `OPENAI_API_KEY` and exfiltrate them via `ATTACH ... TOKEN`.
  (3) The URL passed to `ATTACH` is now single-quote-escaped on both
  paths. Also fixed a `table_schema` vs `table_catalog` mismatch that
  silently no-op'd `_attach_remote_extensions` for every connector
  (the rebuild-path hardening would have been moot in production
  without this fix). New module `src/orchestrator_security.py`
  centralises the policy and exposes `log_effective_policy()`, called
  from app startup so an operator's typo in
  `AGNES_REMOTE_ATTACH_EXTENSIONS` (which **replaces** the default,
  not extends it — a setting of `httpfs` would silently lock out
  `keboola, bigquery`) is visible at boot rather than at the next
  failed attach. See
  `docs/superpowers/plans/2026-04-27-issue-81-trust-boundary.md`.
- **Security (MEDIUM)**: extractor-side identifier validation (issue
  #81 Group D / M15). The Keboola and BigQuery extractors interpolate
  `table_name`, `bucket` / `dataset`, and `source_table` from
  `table_registry` directly into `CREATE OR REPLACE VIEW`,
  `INSERT INTO _meta`, and `COPY ... TO` SQL. Anyone with write access
  to `table_registry` (admin, registry-write API) could inject SQL via
  these identifiers. New shared module `src/identifier_validation.py`
  exposes a strict `validate_identifier` (for our own view names —
  `^[a-zA-Z_][a-zA-Z0-9_]{0,63}$`, used for `table_name` so it matches
  the orchestrator's rebuild-time check and dashed names fail fast at
  extraction rather than being silently dropped at rebuild) and a
  relaxed `validate_quoted_identifier` (for upstream-typed names like
  Keboola `in.c-foo` / BigQuery `my-dataset`:
  `[a-zA-Z0-9_][a-zA-Z0-9_.\-]*`, refusing any character that could
  close a `"..."` identifier literal). The orchestrator's existing
  `_validate_identifier` was lifted into the new module so both layers
  share a single source of truth; both extractors skip-and-continue on
  unsafe rows (logged + counted in failure stats; the rest of the
  registry still processes).

### Removed

- Customer-specific manual-deploy helper `scripts/grpn/Makefile` and its
  README, plus the corresponding hackathon deploy log under
  `docs/superpowers/plans/2026-04-22-grpn-deploy-learnings.md`. These
  documented one operator's hand-rolled stopgap for an org-policy-blocked
  Terraform flow and do not belong in vendor-neutral OSS.
- `scripts/switch-dev-vm.sh` — hackathon-era helper hardcoded to a specific
  shared dev VM. Per-developer dev VMs are
  the supported pattern now; operators who need an equivalent should use
  `gcloud compute ssh <vm> --command "sed -i …/.env && sudo /usr/local/bin/agnes-auto-upgrade.sh"`
  with their own VM details.

### Internal

- Sandbox blocklist now flags introspection-chain dunders explicitly:
  `__subclasses__`, `__globals__`, `__class__`, `__base__`, `__bases__`,
  `__mro__`, `__dict__`, `__code__`, `__builtins__`. `__init__` and
  `__getattribute__` are intentionally **not** in the list — substring match
  would flag every legitimate `def __init__(self):`. The chain breaks at
  the next link anyway.
- New regression test `test_run_pwn_payload_blocked` parametrized over the
  exact PoC from issue #44 plus two equivalent variants (lambda+`__globals__`,
  `__mro__` traversal). If the dunder list is silently weakened in a future
  refactor, the test fails. New `test_*_requires_admin` tests parametrized
  over all three non-admin core roles (analyst, viewer, km_admin).
- `tests/conftest.py::seeded_app` extended with `viewer_token` and
  `km_admin_token` so role-gating tests cover all four core roles.

### Migrated

- **Schema bumped from v9 to v10**. Auto-migration applies on next start
  (creates the `view_ownership` table; data on disk is unaffected). The
  pre-migration snapshot machinery (added at v8→v9) covers v9→v10 too —
  if anything goes wrong during the migration, the snapshot at
  `<DATA_DIR>/state/system.duckdb.pre-migrate` lets you roll back.

## [0.11.5] — 2026-04-27

Follow-up release for PR #73: addresses four rounds of Devin AI review on the role-management-complete branch. No new public-API surface; the user-visible payoff is that v8→v9-migrated installations now work end-to-end (login flows, user list, admin nav, privilege revocation), and `make local-dev` startup is finally quiet.

### Fixed

- **Privilege retention after grant revocation via the new REST API** (Devin review #73). `_hydrate_legacy_role` previously short-circuited on a truthy `user.get("role")`. The role-management endpoints (`POST/DELETE /api/admin/users/{id}/role-grants`, plus the `changeCoreRole` UI flow) only mutate `user_role_grants` — they don't touch the legacy `users.role` column. After a downgrade-via-API, the stale legacy value would keep `user["role"] = "admin"` in memory; `_is_admin_user_dict` and the catalog/sync admin-bypass short-circuits then silently retained elevated table access even though `require_internal_role` correctly denied the API gates. Fix: always re-resolve from `user_role_grants` regardless of the legacy column, making the grants table the single source of truth on every authenticated request. Cost: one DB round-trip per request (same as the existing PAT-aware fallback).
- **Dev-bypass + OAuth callback dropped direct grants from the session cache** (Devin review #73). Both call sites passed `external_groups` only to `resolve_internal_roles`, never the user's id — so `user_role_grants` rows were resolved on the per-request DB-fallback path inside `require_internal_role` instead of the cache. Functionally correct, but every admin-gated request paid a DB round-trip and the dev-bypass log line read "resolved 0 internal role(s)" for an obviously-admin user, which was confusing during debugging. Fix: pass `user_id` so the cache reflects the union at sign-in.
- `GET /api/users` returned **HTTP 500** for any v8→v9-migrated installation. The migration NULL-s legacy `users.role` (kept as a deprecated artifact because DuckDB FK blocks DROP COLUMN), but `UserResponse.role` is a required `str` Pydantic field — every user listing failed validation. `/admin/users` showed only "Failed to load users" and the new `/admin/users/{id}` Detail link was unreachable. Fix: route every user dict returned by the API through `_hydrate_legacy_role` (same shim already used by `get_current_user`), which derives the legacy enum value from `user_role_grants` for migrated users. Also fixes a quieter dual of the same bug — `target["role"] == "admin"` short-circuits in `update_user`/`delete_user` would silently no-op on migrated admins, letting the operator demote/delete the last admin against the documented protection.
- **Scheduler log-noise**: every cron tick produced a `POST /auth/token 401 Unauthorized` access-log line because the scheduler's auto-fetch fallback was always broken — it called `/auth/token` with just an email, but the endpoint requires email + password. Fix: removed the auto-fetch path entirely. Operators set `SCHEDULER_API_TOKEN` (a long-lived PAT) in production; in `LOCAL_DEV_MODE` the dev-bypass auto-authenticates the un-tokenized request, so jobs continue to work.
- **HTTP 500 on `POST /auth/token` for v8-migrated users** (Devin review #73 round 3). `TokenResponse.role` is a required `str` Pydantic field, but the v8→v9 migration NULL-s the legacy `users.role` column for every existing user. The login endpoint passed the raw NULL through to Pydantic, raising `ValidationError` → 500. Same root cause produced semantically wrong (but non-crashing) JWTs from Google OAuth, password, and email-magic-link flows — they wrote `role: null` into the issued token; downstream `_hydrate_legacy_role` in `get_current_user` would correct the per-request view, but the token payload itself stayed misleading. Fix: hydrate inline in each login flow before reading `user["role"]` — `app/auth/router.py` (`POST /auth/token`), `app/auth/providers/google.py` (OAuth callback), `app/auth/providers/password.py` (5 flows: JSON login, web login, JSON setup, web reset, web setup), and `app/auth/providers/email.py` (centralized in `_consume_token`, covers both magic-link `/verify` endpoints). New regression class `TestAuthLoginFlowsPostMigration` in `tests/test_schema_v9_migration.py` pins both the no-crash and the correct-role contracts for all four legacy levels (viewer/analyst/km_admin/admin).
- **`docs/RBAC.md` documented an `implies=[…]` keyword on `register_internal_role()` that the function doesn't accept** (Devin review #73 round 3). A module author copying the example would hit `TypeError: got an unexpected keyword argument 'implies'` at import time. Reality: `implies` is currently seeded only for the `core.*` hierarchy via `_seed_core_roles` in `src/db.py` — the registry-side write path doesn't exist yet. Rewrote the *Implies hierarchy* and *Module-author workflow* sections to document what's actually supported in 0.11.4 and what a future change would need to add.
- **`_seed_core_roles` was advertised as a per-connect safety net but only ran during fresh installs and the v8→v9 migration** (Devin review #73 round 4). The docstring promised "called from `_ensure_schema` on every connect" so an accidental `DELETE FROM internal_roles WHERE key = 'core.admin'` (or a doc-tweak release that updated `_CORE_ROLES_SEED` without bumping the schema version) would self-heal on the next process start. In reality both call sites lived inside `if current < SCHEMA_VERSION:` — once the DB was on v9, the seed function never ran again, leaving any deletion permanent and any in-code `display_name`/`description`/`implies` change requiring a manual SQL deploy. Fix: added an unconditional tail call to `_seed_core_roles(conn)` at the bottom of `_ensure_schema`, gated only by `current <= SCHEMA_VERSION` so the future-version-rollback contract still holds. New regression class `TestSeedCoreRolesSafetyNet` in `tests/test_schema_v9_migration.py` pins all three contracts (deleted row re-seeds, mutated `display_name` re-syncs from code, `applied_at` doesn't churn on already-current DBs).
- **`make local-dev` startup spammed an `AuthlibDeprecationWarning` from upstream's own `_joserfc_helpers.py`** every time `app/auth/providers/google.py` triggered the `from authlib.integrations.starlette_client import OAuth` import chain. The warning is upstream-internal — authlib telling itself to migrate from `authlib.jose` to `joserfc` before its 2.0 cut — and isn't actionable on our side until either authlib ships the fix or we rewrite OAuth on top of `joserfc` directly. Filtered the specific warning class at the top of `app/main.py` (with a message-based fallback if the class moves in a future authlib release) so the warning no longer pollutes operator-facing stdout. Other `DeprecationWarning`s remain visible.

### Added

- **`/profile` now self-services every user's role situation.** Three new sections rendered server-side for *all* signed-in users (not just admins): *Effective roles* (the full resolver output as chip cloud — direct grants ∪ group-derived ∪ implies-expanded), *Direct grants* (rows in `user_role_grants` with source label: `auto-seed` from v8 backfill vs. `direct` admin grant), and *Roles via groups* (which Cloud Identity / dev group grants which role for the current user). Non-admins finally see *why* a particular feature is or isn't accessible without asking an admin to read the DB. Admins additionally see a deep-link to `/admin/users/{id}` for editing their own grants in place.
- **`/admin/role-mapping` group ID picker.** A new "Known groups" panel above the create-mapping form surfaces clickable chips of group IDs known to the system: the calling admin's own `session.google_groups` (with human-readable names + a "your group" tag) merged with distinct `external_group_id`s already used in existing mappings (tagged "already mapped"). Click a chip → fills the form's external-group-id input and focuses the role select. Empty-state copy points the operator at `LOCAL_DEV_GROUPS` / Google sign-in when the picker is empty, instead of leaving them to guess Cloud Identity opaque IDs from memory.

### Changed

- Renamed `docs/internal-roles.md` → **`docs/RBAC.md`**. Standard industry term, more discoverable for engineers grepping for "RBAC" in a new repo. Added Quickstart-by-role sections (operator / end-user / module author) and a step-by-step *Module-author workflow* with code examples for registering a key, gating endpoints, declaring implies hierarchies, and writing a contract test against the gate. Cross-references in code (`app/api/admin.py`, `tests/test_role_resolver.py`) updated. `CLAUDE.md` now points contributors at the new doc from the *Extensibility → RBAC* section. Historical CHANGELOG entries (`[0.11.3]` / `[0.11.4]` body) keep the original `internal-roles.md` filename — they describe what shipped at that version and aren't retro-edited.

## [0.11.4] — 2026-04-27

Role-management complete release. Sjednocuje legacy `users.role` enum (viewer/analyst/km_admin/admin) with the v8 internal-roles foundation under one model with implies hierarchy, ships admin UI + REST API + CLI for managing both group mappings and direct user grants, and wires `require_internal_role` for PAT-aware resolution so admin endpoints work uniformly across OAuth and headless callers.

### Added

- **Schema v9 — unified role model.** New `user_role_grants(user_id, internal_role_id, granted_by, source)` table for direct user→role assignments (complementary to `group_mappings` which assigns via Cloud Identity group). Two new columns on `internal_roles`: `implies` (JSON array of role keys this role transitively grants) and `is_core` (BOOL, distinguishes seeded core.* hierarchy from module-registered roles). Migration v8→v9 seeds four `core.*` rows (`core.viewer/analyst/km_admin/admin`) with the legacy hierarchy as `implies` (`core.admin → core.km_admin → core.analyst → core.viewer`), backfills one `user_role_grants` row per existing user mirroring their pre-v9 `users.role` value (`source='auto-seed'`), and NULLs the legacy column.
- **PAT-aware `require_internal_role`.** Two-path resolution: session cache first (OAuth flow), DB-backed `user_role_grants` fallback (PAT/headless flow). Admin CLI scripts now hit gated endpoints uniformly without an OAuth round-trip. The PAT-specific 403 message from 0.11.3 is removed — PAT now legitimately resolves through direct grants.
- **Implies expansion at resolve time.** New `expand_implies(role_keys, conn)` helper in `app.auth.role_resolver` does BFS over the `implies` graph; `resolve_internal_roles` calls it at the end so a single `core.admin` grant expands to the full four-level hierarchy automatically.
- **Dotted role-key namespace.** Regex extended to allow `core.admin`, `context_engineering.admin`, `corporate_memory.curator` style keys (max 64 chars, lower-snake-case segments separated by dots). The owner_module column should match the prefix before the first dot.
- **REST API for role management.** New router `app/api/role_management.py` under `/api/admin`: `GET/POST/DELETE` on `group-mappings`, `users/{id}/role-grants`, plus `GET internal-roles` and `GET users/{id}/effective-roles` (debug). All gated by `require_internal_role("core.admin")` — works for both OAuth admins (cookie) and admin PATs.
- **Admin UI `/admin/role-mapping`.** Browse internal roles, manage Cloud Identity group → role mappings (table view + create/delete forms). User detail page extended with three sections: *Core role* (single-select for `core.*`), *Additional capabilities* (multi-checkbox for module roles), *Effective roles* (debug view of direct + group-derived + expanded set).
- **`da admin` CLI subcommands.** `role list`, `role show <key>`, `mapping list/create/delete`, `grant-role <email> <key>`, `revoke-role <email> <key>`, `effective-roles <email>`. All run over PAT — use them in CI scripts to grant/revoke roles without going through the browser.

### Changed

- **BREAKING (semantics, not API).** `users.role` column NULL-ed during v8→v9 migration. Reads via `UserRepository.get_by_*` still return the column but the value is always NULL after upgrade — code reading `user["role"]` directly in business logic gets `None`. The legacy `Role` enum (`Role.VIEWER/ANALYST/KM_ADMIN/ADMIN`) and convenience helpers (`is_admin`, `has_role`, etc. in `src/rbac.py`) continue to work — they now read from `user_role_grants` via the resolver. Sweeping `user.get("role") == "admin"` checks were rewritten to the new helper. The column itself is preserved physically because DuckDB rejects DROP COLUMN while a FK references the table; physical drop is deferred to a future schema-rebuild migration.
- `require_role(Role.X)` and `require_admin` are now thin wrappers over `require_internal_role(f"core.{role}")`. Behavior identical for OAuth users (admin role from group_mappings); PAT users now succeed when they hold a direct `core.admin` grant.
- `UserRepository.create()` and `update()` mirror role changes into `user_role_grants` automatically (`_grant_core_role` helper); existing setup code keeps working without changes.
- `UserRepository.delete()` pre-deletes `user_role_grants` rows (DuckDB FK doesn't auto-cascade).
- `UserRepository.count_admins()` reads `user_role_grants ⨝ internal_roles WHERE key='core.admin'` — the legacy `users.role = 'admin'` count would always return 0 after backfill.
- `app/api/admin.py` module-level docstring documents the v9 pattern for module authors who want to add their own capability gates.
- `docs/internal-roles.md` rewritten to remove the v8 "no UI yet" caveat, document the implies hierarchy, the dual session/DB resolution pathway, and the dotted-namespace key convention.

### Removed

- `require_internal_role`'s session-only enforcement (the v8 *"This endpoint needs an interactive (OAuth) session — Bearer/PAT tokens do not carry session-resolved roles"* error message). PAT clients with a matching `user_role_grants` row now pass the gate uniformly.

### Internal

- New `UserRoleGrantsRepository` in `src/repositories/user_role_grants.py` mirrors the style of `GroupMappingsRepository` (list/get/create/delete + per-user / per-role indices).
- INFO-level audit log on grant + mapping mutations (action strings: `role_mapping.created/deleted`, `role_grant.created/deleted`, resource `mapping:<id>` / `grant:<id>`).
- "Last admin protection" on `DELETE /api/admin/users/{id}/role-grants/{grant_id}`: refuses to delete the final `core.admin` grant in the system (mirrors existing `count_admins` protection on user deletion / deactivation).

## [0.11.3] — 2026-04-26

Authorization-foundation release — adds the internal-roles layer between Cloud Identity groups and per-module capability checks. Schema v8 migration; no admin UI yet (follow-up).

### Added

- **Internal roles + group mapping (foundation).** Schema v8 adds two tables: `internal_roles` (app-defined capabilities like `context_admin`, `agent_operator`, registered by Agnes modules at import time) and `group_mappings` (many-to-many bindings of Cloud Identity group IDs to internal role keys, managed by admins). New `app.auth.role_resolver` module exposes `register_internal_role(...)` for module authors, `sync_registered_roles_to_db(...)` (run once at startup, idempotent), `resolve_internal_roles(external_groups, conn)` (called at sign-in, writes resolved keys into `session["internal_roles"]`), and a `require_internal_role("…")` FastAPI dependency factory for permission checks. Resolution runs at sign-in (Google OAuth callback + dev-bypass — populates on first request and whenever external groups change, mirroring the OAuth callback's always-write semantics). No DB hit per request. Refresh requires re-login, same semantics as `session.google_groups`. **No admin UI yet** — mapping rows must be created via the repository directly until the management UI ships in a follow-up. PAT/headless clients carry no session and therefore cannot pass `require_internal_role` gates by design — `require_internal_role` distinguishes "signed-in but missing role" from "no session at all" and surfaces a PAT-specific 403 detail in the second case so an API consumer hitting the wall sees what to fix. See `docs/internal-roles.md` → *PAT and headless requests*.

### Changed

- `docs/internal-roles.md` documents `Admin → Users → deactivate then reactivate` as the supported "force re-resolve now" lever for users you can't get to log out (long-lived sessions, automated clients) — invalidates the existing session and forces a fresh sign-in on the next request.

### Internal

- INFO-level audit log on every successful resolve (OAuth callback + dev-bypass) so a "wrong role" complaint is debuggable from the log alone — admin can correlate "user X claims they lost access" with the resolver output without replaying the request.
- Startup warning when `SESSION_SECRET` is shorter than 32 chars, matching the existing `JWT_SECRET_KEY` gate. Both HMAC surfaces sign trust-laden state (`session.internal_roles`, `session.google_groups`, JWTs) — keeping the two gates consistent so a weak secret gets surfaced at boot, not after a quiet downgrade.
- `_clear_registry_for_tests()` now refuses to run unless `TESTING=1` so a stray import path in production can't drop the registered capabilities.

## [0.11.2] — 2026-04-26

Dev-experience patch release — make `LOCAL_DEV_MODE` realistic enough to actually exercise group-aware code paths on `localhost`, and consolidate scattered dev-onboarding instructions into a single `docs/local-development.md`.

### Added

- **`LOCAL_DEV_GROUPS` env var** mocks `session.google_groups` for the auto-logged-in dev user when `LOCAL_DEV_MODE=1`. JSON array matching the production shape (`[{"id":"…","name":"…"}]`) so group-aware UI and access-control code paths can be exercised on `localhost` without a Google OAuth round-trip. Honored only under `LOCAL_DEV_MODE=1`. The startup banner reports the parsed group IDs (or warns loudly when the value is set but malformed), so a typo gets surfaced at boot rather than silently on the first authenticated request. Session injection mirrors the production OAuth callback's "always-write" semantics — including clearing stale groups when the operator unsets `LOCAL_DEV_GROUPS` mid-session. See `docs/auth-groups.md` → *Local-dev mock*.
- **`make local-dev` now seeds two default mocked groups** (`Local Dev Engineers` + `Local Dev Admins` on `example.com`) via `scripts/run-local-dev.sh`, so first-boot `/profile` is non-empty out of the box. Override with `LOCAL_DEV_GROUPS='[…]' make local-dev`; disable with `LOCAL_DEV_GROUPS= make local-dev`.
- **`docs/local-development.md`** — single onboarding doc for working on Agnes locally: TL;DR, what `LOCAL_DEV_MODE` actually bypasses, group mocking, what isn't mocked, and the security-rails reminder that dev mode must never reach a production deploy.

### Internal

- Fix nightly `docker-e2e` CI failures: refresh two stale assertions that had drifted from the live API. `tests/test_docker_full.py::test_app_returns_html_on_root` now expects the auth-aware `302 → /login` (root has redirected since the auth middleware landed); `tests/test_e2e_docker.py::TestDockerHealth::test_health_has_duckdb` now reads `services["duckdb_state"]` (current health-payload shape, already validated by `tests/test_api.py`). No application behavior change — these only ran in the scheduled nightly job, so the drift went unnoticed for several PRs.

## [0.11.1] — 2026-04-26

Patch release — hotfix the missed Caddy env passthrough that should have shipped with 0.11.0, plus codify changelog discipline so this kind of drift gets caught at PR review time next time.

### Fixed

- `docker-compose.yml` caddy service now passes `CADDY_TLS` through to the container (`- CADDY_TLS` bare-form passthrough). Without it the `Caddyfile` `{$CADDY_TLS:default}` substitution always falls back to cert-file mode regardless of what the operator wrote into `.env`, and Caddy crash-loops on Let's Encrypt / internal-CA deployments. Should have shipped with #52; first attempt was #55, accidentally closed before merging.

### Internal

- `CLAUDE.md` — non-negotiable changelog discipline: every PR touching user-visible behavior must update `CHANGELOG.md` under `## [Unreleased]` in the same PR.

## [0.11.0] — 2026-04-26

First tagged semver release. The `version = "2.x"` strings that appeared in earlier `pyproject.toml` snapshots were arbitrary placeholders from the initial scaffold and never reflected actual API maturity — resetting to pre-1.0 to signal that things may still shift.

### Added — Auth

- **Google Workspace groups on `/profile`.** OAuth callback fetches the signed-in user's group memberships via Cloud Identity (`searchTransitiveGroups` with the `security` label — see `docs/auth-groups.md` for the GCP setup checklist and the `security`-vs-`discussion_forum` gotcha). Profile link added to the user dropdown.
- **Password reset + invite flows** for web and admin (`/auth/password/reset`, `/admin/users/invite`).
- **Personal access tokens (PAT)** with separate `:typ=pat` JWT claim, per-token revoke, last-used IP tracking, "My tokens" + admin "All tokens" UI.
- **Email magic-link provider** (itsdangerous-signed token).
- **Optional `SEED_ADMIN_PASSWORD`** to pre-hash the seed admin (dev convenience).

### Added — Deploy

- **`keboola-deploy.yml` workflow.** Tag-triggered alternative to `release.yml` for shared dev VMs that want explicit "deploy when I tag" semantics. Publishes immutable `:keboola-deploy-<tag>` + floating `:keboola-deploy-latest` alias.
- **Caddy + Let's Encrypt + corporate-CA TLS.** `Caddyfile` parametrized via `$CADDY_TLS` env var so a single file serves three regimes: cert-file (corp PKI), Let's Encrypt auto-issue, Caddy-internal-CA. URL-driven cert rotation with self-signed fallback (`scripts/grpn/agnes-tls-rotate.sh`). `docker-compose.tls.yml` overlay closes host `:8000` when Caddy fronts.
- **`dev_instances` schema in `customer-instance` Terraform module** gains optional `tls_mode` + `domain` (mirrors `prod_instance`). `infra-v1.6.0` tag.
- **Optional Google OAuth credentials from Secret Manager.** Module reads `google-oauth-client-{id,secret}` at boot if present; graceful fallback so non-Google deployments aren't affected.
- **`LOCAL_DEV_MODE` + `make local-dev-up` / `local-dev-down`** for one-keystroke local stack with magic-link auth pre-wired.
- **Per-developer `dev-<prefix>-latest` GHCR alias** for branches matching `<prefix>/<branch>` — push-to-deploy on personal dev VMs.
- **`/setup` web wizard** for first-time instance setup, plus headless `POST /api/admin/configure` and `POST /api/admin/discover-and-register`.
- **Smoke-test job in CI** (Docker-in-CI after every release) + `scripts/smoke-test.sh` for post-deploy verification.

### Added — CLI

- **Wheel distribution** + auto-update check on startup.
- `--version` flag, `--dry-run` + `X/N` progress on `da sync`, durable sync (atomic writes + manifest hash + retry on transient errors).
- gzip on JSON/HTML responses (server-side).

### Added — Data

- **Remote query engine.** Two-phase BigQuery + DuckDB engine for tables too large to sync locally (`--register-bq` flag).
- **Business metrics.** Standardized `metric_definitions` table in DuckDB with starter pack importer (`da metrics import`).
- **`/api/health`** returns `version`, `channel`, `commit_sha`, `image_tag`, `schema_version`.
- **Custom connector mount support** (`connectors/custom/`).
- **OpenAPI snapshot test** for breaking-change detection.

### Added — Docs / tooling

- `docs/auth-groups.md`, `docs/DEPLOYMENT.md`, `docs/HACKATHON.md`, `docs/ONBOARDING.md` runbooks.
- `scripts/debug/probe_google_groups.py` — stdlib-only probe for diagnosing Cloud Identity API issues without a deploy cycle.
- Schema migration safety tests (idempotency, data preservation, snapshot).
- Pre-migration snapshot of `system.duckdb` before schema upgrades.
- Auto-generated JWT and session secrets with file persistence (`/data/state/.jwt_secret`).
- Startup banner logging version, channel, and schema version.

### Changed

- **BREAKING (deployment)** — Caddy compose profile renamed `production` → `tls`. Existing `docker compose --profile production up -d` invocations need to switch.
- **BREAKING (deployment)** — Default `Caddyfile` mode is now cert-file (`tls /certs/fullchain.pem /certs/privkey.pem`); for the previous Let's Encrypt auto-issue behaviour set `CADDY_TLS=tls <ops-email>` in `.env`. See `docs/auth-groups.md` and `Caddyfile` inline docs.
- Schema migration v5→v6→v7: adds `users.active`, `personal_access_tokens` table, `personal_access_tokens.last_used_ip`. Auto-applied at boot.
- Image-level `AGNES_VERSION` now sourced from `pyproject.toml` at build time (no more drift between `da --version` and the package metadata).
- **Vendor-agnostic OSS rule** codified in `CLAUDE.md` — customer-specific names, hostnames, project IDs belong in consumer infra repos, not in this OSS distribution.

### Fixed — Security

- Open-redirect guard for backslash in `safe_next_path`.
- `SessionMiddleware max_age=3600 + https_only` (was browser-session forever, plain-HTTP-OK).
- Timezone-aware datetimes in Keboola metadata cache.
- Atomic magic-link token consumption (closes double-use race under concurrent clicks).
- Bootstrap backdoor closed when passwordless seed admin exists.
- urllib3 1.26→2.6.3 (resolves 4 Dependabot security alerts).
- argon2-cffi adopted for password hashing.
- See [docs/security-audit-2026-04.md](docs/security-audit-2026-04.md) for the full audit (renamed from `docs/padak-security.md` in #94).

### Fixed — Other

- `uvicorn --proxy-headers --forwarded-allow-ips='*'` so OAuth callbacks resolve to https when behind a TLS terminator.
- `scripts/grpn/agnes-tls-rotate.sh` hardened: `--max-redirs 0` + `--proto '=https'` on cert fetch, post-fetch PEM validation (rejects HTML error pages from corp portals), `ulimit -c 0` to suppress coredumps that could leak the unencrypted privkey, POSIX-safe `${arr[@]+"${arr[@]}"}` array expansion.
- `scripts/tls-fetch.sh` — generic URL fetcher (`sm://`, `gs://`, `https://`, `file://`) with redirect refusal + PEM validation.
- `kbcstorage` moved to optional dep — unblocks urllib3 security updates; primary Keboola path now uses the DuckDB Keboola extension.
- Dependencies consolidated into `pyproject.toml` (no more `requirements.txt`).

### Internal

- Test suite expanded to 1357+ tests (4 layers — unit, integration, web smoke, journey).

[0.11.3]: https://github.com/keboola/agnes-the-ai-analyst/releases/tag/v0.11.3
[0.11.2]: https://github.com/keboola/agnes-the-ai-analyst/releases/tag/v0.11.2
[0.11.1]: https://github.com/keboola/agnes-the-ai-analyst/releases/tag/v0.11.1
[0.11.0]: https://github.com/keboola/agnes-the-ai-analyst/releases/tag/v0.11.0
