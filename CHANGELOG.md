# Changelog

All notable changes to Agnes AI Data Analyst.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html), pre-1.0 — public surface (CLI flags, REST endpoints, `instance.yaml` schema, `extract.duckdb` contract) may shift between minor versions; breaking changes called out under **Changed** or **Removed** with the **BREAKING** marker.

CalVer image tags (`stable-YYYY.MM.N`, `dev-YYYY.MM.N`) are produced for every CI build; semver tags (`v0.X.Y`) are cut at release boundaries and reference the same commit as a `stable-*` tag from the same day.

---

## [Unreleased]

<!-- Add bullets here. Group: Added / Changed / Fixed / Removed / Internal.
     Mark breaking changes with **BREAKING** at the start of the bullet. -->

### Changed

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
  instead.

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

### Changed (ops)

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
