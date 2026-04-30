# Changelog

All notable changes to Agnes AI Data Analyst.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html), pre-1.0 — public surface (CLI flags, REST endpoints, `instance.yaml` schema, `extract.duckdb` contract) may shift between minor versions; breaking changes called out under **Changed** or **Removed** with the **BREAKING** marker.

CalVer image tags (`stable-YYYY.MM.N`, `dev-YYYY.MM.N`) are produced for every CI build; semver tags (`v0.X.Y`) are cut at release boundaries and reference the same commit as a `stable-*` tag from the same day.

---

## [Unreleased]

## [0.24.0] — 2026-04-30

### Changed

- **Effective-access readout no longer short-circuits for admin users on `/admin/users/{id}` and `/profile`.** Both `GET /api/admin/users/{id}/effective-access` and `GET /api/me/effective-access` previously returned `is_admin=true, items=[]` when the target was in the Admin group, and the UI rendered a flat "Full access via Admin" gold pill — which hid the underlying grant graph. Now both endpoints always run the JOIN, return the explicit per-resource breakdown, and surface `is_admin` only as informational metadata on the response. The UI drops the special pill on both surfaces and renders the same per-resource table everyone else sees. Authorization at runtime still gives Admin god-mode regardless of this list (see `app.auth.access.is_user_admin`); this is purely an audit/debug surface for admins to see *which* Admin-group grants exist via *which* sibling groups.

- **`/profile` group memberships use the same color-coded chip vocabulary as the rest of the admin surface.** Each membership renders as a colored `.group-chip` (Admin yellow, Everyone gray, google_sync green, custom purple) with the same name-shortening rule (`grp_acme_legal@workspace.example.com` → `Legal`, full email on hover via `title`). The Status row in the Account card was removed — same admin signal already appears as the Admin chip in Group memberships, so the pill was redundant. Server-side: the `/profile` route now projects `origin` and `display_name` per membership (computed via the shared `_derive_origin` helper + the `AGNES_GOOGLE_GROUP_PREFIX` strip), so the Jinja template stays env-lookup-free.

- **`/admin/users/{id}` polish: header `Admin` pill removed, "Add to group" dropdown filters out google-managed groups, whole user-cell on the list page is one anchor.** Header pill was redundant — the Group memberships section already shows the Admin chip with the canonical yellow color. The dropdown now skips `is_google_managed` rows (both `created_by='system:google-sync'` and the env-mapped Admin/Everyone) so admins don't see options the API would 409 on anyway. On `/admin/users` the avatar + name + email block became a single `<a class="user-cell">` linking to `/admin/users/{id}` so the entire info area lights up on hover, not just one line; the dedicated `Detail` action button stays for explicit affordance.

- **`/admin/users/{id}` Group memberships table renders chips with the same color + name-shortening rules as the user list.** The Group cell is now a `<a class="group-chip">` colored by `is-admin` (yellow) / `is-everyone` (gray) / `is-google_sync` (green) / `is-custom` (purple) and links through to `/admin/groups/{group_id}`. Google-sync chip text shortens via `deriveDisplayName` (e.g. `grp_acme_legal@workspace.example.com` → `Legal`); raw email lives on the chip's `title` attribute. Powered by a new `origin` field on `UserMembershipResponse` (`GET /api/admin/users/{id}/memberships`), computed via the same `_derive_origin` helper the rest of the surface uses.

- **`/admin/users` membership chips are color-coded by group origin and shorten Workspace-email names to a friendly form, so a row tells the same story as `/admin/groups` at a glance.** Colors: Admin → yellow, Everyone → gray, other google-synced groups → green, admin-created custom groups → purple. Name match (`Admin` / `Everyone`) takes precedence over origin so an env-mapped Admin/Everyone row (whose API origin is `google_sync`) keeps its canonical color. The chip text for google_sync groups runs through the same `deriveDisplayName` helper used on `/admin/groups`: `grp_acme_legal@workspace.example.com` renders as `Legal` (prefix stripped via `AGNES_GOOGLE_GROUP_PREFIX`, capitalized), and the raw Workspace email goes into the chip's `title` attribute for hover reveal. Custom / Admin / Everyone chip text stays raw — `deriveDisplayName` would over-capitalize names like `data-team`. To support this, `GroupBrief` on `GET /api/users` now carries the same `origin` field as `/api/admin/groups`, computed via the shared `_derive_origin` helper. Replaces the v12-era 2-color layout (yellow Admin, gray for any other system row, blue for everything else, full email always shown) which gave no signal about whether a chip came from Workspace or a manual admin grant and overflowed the cell on long Workspace emails.

- **`/admin/access` sidebar + right-pane title now use the same group display rules as `/admin/groups`.** Each sidebar row renders a multi-color origin pill (`google_sync` / `system` / `custom`) instead of the legacy yellow inline `system` tag, and a monospace subtitle below the name showing the Workspace email when the row is wired to one (`mapped_email` for env-mapped Admin/Everyone, the raw `name` for user-created google-sync groups). The right-pane card head adopts the same treatment when a group is selected. To support this, `GET /api/admin/access-overview` now includes `origin`, `mapped_email`, `is_google_managed`, and `created_by` per group — single source of truth shared with the `GET /api/admin/groups` endpoint via the same helpers (`_derive_origin`, `_mapped_email`, `_is_google_managed`).

- **`GET /api/admin/groups` and `GET /api/admin/access-overview` rename the `origin` value `"admin"` → `"custom"`.** The label is named after the row's *origin* (admin-created via UI/CLI), not the creator's role, so the pill doesn't visually clash with the seeded `Admin` system group's name. CSS class `.origin-admin` → `.origin-custom`; same purple swatch. No external consumers (CLI never reads the field). Pydantic default and JS fallbacks updated in lock-step. The previous workaround — a frontend `originLabel()` helper that mapped `admin → Custom` at render time — is gone now that the API value already reads correctly.

- **`/admin/groups` switches the seeded Admin / Everyone rows to a `google_sync` chip and shows the Workspace email as a subtitle when env-mapped.** Previously the mapped Admin row showed `Admin` as the big title with `Admin` repeated as the subtitle (the `deriveDisplayName` strip-and-capitalize chain produced no useful output for a literal canonical name) and a yellow `system` chip — which buried the fact that membership is actually owned by Workspace. Now: when `AGNES_GROUP_ADMIN_EMAIL` / `AGNES_GROUP_EVERYONE_EMAIL` is configured, `GET /api/admin/groups` reports `origin='google_sync'` for the matching seeded row (the system badge is suppressed; Workspace is the authoritative source of membership) and the new `mapped_email` field carries the configured Workspace email. The list view shows the canonical name as the big title with the Workspace email as a monospace subtitle (`Admin / admins@workspace.test`) and a green `google_sync` chip. The `/admin/groups/{id}` detail header mirrors the same — name as `<h1>`, `mapped_email` as the `gd-title-email` subtitle. Unmapped Admin / Everyone rows stay `origin='system'` with no subtitle. Regular google_sync rows (whose `name` is already the Workspace email) keep the existing `deriveDisplayName` rewrite behavior with `mapped_email=null`.

- **SSO-managed accounts are read-only for password / delete operations, both in UI and at the API layer.** Detection is in `app.api.users._is_sso_user`: a user counts as SSO-managed if they belong to any group whose `created_by = 'system:google-sync'`, OR they belong to the seeded `Admin` system group while `AGNES_GROUP_ADMIN_EMAIL` is set, OR the seeded `Everyone` system group while `AGNES_GROUP_EVERYONE_EMAIL` is set. Users with no groups, or only admin-created custom groups, are unaffected. The flag surfaces as `is_sso_user: bool` on every `/api/users` and `/api/users/{id}` response. UI: the `/admin/users` row actions and the `/admin/users/{id}` Account section suppress the Reset / Set pwd / Delete buttons for those rows. Server: `POST /api/users/{id}/reset-password`, `POST /api/users/{id}/set-password`, and `DELETE /api/users/{id}` now return **409** with `detail: "User is managed by an external SSO provider; …"` for SSO targets — so a curl-savvy admin who bypasses the UI guard still cannot reset / set / wipe a Google Workspace account locally. Deactivate stays available so admins can gate access locally even when the upstream account is managed elsewhere. Name is provider-neutral so a future provider (Cloudflare Access, Okta, …) plugs into the same flag without churning the API.

### Fixed
- **`scripts/ops/agnes-tls-rotate.sh` now chowns `/data/state/certs/` to UID 999 (the `agnes` user inside the app image) on every run.** Previously the script only `mkdir -p`'d and `chmod 700`'d the directory, leaving ownership to whoever happened to create it first — root when systemd fired the timer before docker-compose-up, or UID 999 when the container's volume init touched it first. Race-dependent. When root won, the resulting `drwx------ root:root` directory was unreadable by the UID-999 container, `_read_agnes_ca_pem()` returned `None`, and the `/install` setup prompt silently dropped the cross-platform TLS trust block (Step 0 from #137) — operators on those VMs ended up with no client-side cert bootstrap and a broken `claude plugin marketplace add` against the self-signed host. The chown is unconditional + idempotent (`|| true` for hosts where the numeric GID can't be set), so re-running the timer self-heals existing VMs without manual `chown` on the operator's part. Files inside the directory keep their existing modes — `fullchain.pem` is `0644` (world-readable, so root- or 999-owned both work for the agnes container) and `privkey.pem` is `0600` (only Caddy reads it, and Caddy's container runs as root).
- **`_is_sso_user` no longer treats `system_seed` / `admin` memberships in env-mapped Admin/Everyone as SSO (Devin BUG_0002 on PR #142).** Without checking `user_group_members.source`, the v13 migration's blanket Everyone backfill (`source='system_seed'`) flipped every existing local user to `is_sso_user=True` the moment an operator set `AGNES_GROUP_EVERYONE_EMAIL` — locking the admin out of password reset / set / delete on accounts the IdP doesn't actually own (the admin couldn't even un-flag them via "remove from Everyone" because `_guard_google_managed` blocks manual removal once env-mapped). The system-group branches (Admin / Everyone) now additionally require `source='google_sync'`. The created_by branch (`system:google-sync` groups) is unchanged because those groups only exist because of Google sync — every membership in them is IdP-owned regardless of `source`. The v18 migration in this PR also retroactively cleans up the offending `system_seed` rows in env-mapped Admin/Everyone groups; the source-check fix is the runtime guard that keeps future writes safe.
- **`POST /api/admin/users/{id}/memberships` now returns the correct `origin` for the new membership (Devin review round 1 on PR #142).** The handler constructed `UserMembershipResponse` without setting `origin`, so the model default `"custom"` was returned regardless of the target group — while the matching GET endpoint computes `origin` via the shared `_derive_origin` helper. Adding a user to a system group (Admin / Everyone) over POST now reports `origin="system"` (or `"google_sync"` when env-mapped), matching GET. The UI re-fetches after add so visible impact was zero, but any non-UI API consumer got the wrong value.

- **Schema migration v18: drop stranded non-google memberships in google-managed groups (Devin review round 1 on PR #142, partial response).** v13's `_v12_to_v13_finalize` unconditionally backfilled every existing user into Everyone with `source='system_seed'` under the original "Everyone = all users" semantics. The platform design has since shifted: when `AGNES_GROUP_EVERYONE_EMAIL` / `AGNES_GROUP_ADMIN_EMAIL` is configured, those system rows mirror a Workspace group exclusively, and only Google sync should write into them. The leftover `system_seed` rows (a) misrepresent the membership model and (b) cause `_is_sso_user` to flag local users as SSO-managed, blocking password-reset / set / delete via `_reject_if_sso`. v18 deletes: (1) non-google memberships in auto-created `created_by='system:google-sync'` groups (unconditional — those groups only exist because Workspace materialized them), (2) `system_seed` rows in Everyone **only when `AGNES_GROUP_EVERYONE_EMAIL` is set**, (3) `system_seed` rows in Admin **only when `AGNES_GROUP_ADMIN_EMAIL` is set** AND `added_by NOT IN ('app.main:seed_admin', 'auth.bootstrap')` so the bootstrap admin always survives. Env-conditional branches mean a non-Google deployment keeps its local Admin / Everyone semantics intact (system_seed rows there are legitimate, not cruft). Runtime safeguards against future writes from the legacy `users.role` apparatus are tracked in #144.

### Removed

- **`GET /api/admin/group-suggestions` endpoint and the "Suggested from your Google account" picker on the `/admin/groups` create modal.** The picker fetched the calling admin's Workspace groups (via Cloud Identity), filtered out ones already registered as `user_groups` rows, and offered them as one-click name pre-fills. Replaced by the OAuth callback's automatic `google_sync` group materialization (every Workspace group the user belongs to that matches `AGNES_GOOGLE_GROUP_PREFIX` is auto-created on login) — the manual picker became redundant. Cloud Identity calls in the request path are gone with it.

## [0.23.0] — 2026-04-30

### Added
- **Single-item Edit button on every memory item card** in `/corporate-memory/admin`. Surfaces the per-item `PATCH /api/memory/admin/{id}` endpoint added in #126 — until now it was only reachable via the CLI (`da admin memory edit <id>`) or by selecting one item in the bulk batch bar. The modal pre-fills from the item's current title / content / category / domain (dropdown matching `VALID_DOMAINS` + `(unset)`) / audience / tags (comma-separated). Authorisation: same `require_admin` gate as the rest of the memory admin surface.
- **`ai` section editable in `/admin/server-config`**. The `ai:` block in `instance.yaml` (provider / api_key / model / base_url / structured_output for the corporate-memory extractor) was missing from `_EDITABLE_SECTIONS` and `SECTION_META`, so admins had no UI path to view or set the LLM token without editing `instance.yaml` directly. `api_key` is auto-masked via the existing `_SECRET_KEY_PATTERNS` (substring matches "api_key"), so the input renders as a password field and audit-log diffs redact the value.
- **`MEMORY_DOMAIN` RBAC resource type** for corporate-memory items. Admins use `/admin/access` to grant `user_groups` access to specific domains (one of `finance` / `engineering` / `product` / `data` / `operations` / `infrastructure`). Members of granted groups see all `knowledge_items` in that domain regardless of the existing `audience` string filter. The two filters compose with OR semantics, so the existing `audience='group:X'` convention keeps working unchanged for ad-hoc per-item targeting; pre-grant deployments behave identically (when no MEMORY_DOMAIN grants exist, the OR clause collapses to a no-op). Wired in `KnowledgeRepository.list_items` / `search` / `count_items` / `count_by_tag` / `count_by_audience` and in the inline SQL of `GET /api/memory/stats` via a new `granted_domains` parameter resolved from `resource_grants` by `_caller_granted_memory_domains`. **Note**: a MEMORY_DOMAIN grant is a parallel visibility path that pierces the `audience` field — an item with `audience='group:admins-only'` and `domain='finance'` becomes visible to anyone with a `MEMORY_DOMAIN/finance` grant. Operators who relied on `audience` as a hard access boundary should be aware (Devin ANALYSIS_0003 on PR #141).

### Fixed
- **Edit modal NULL→empty-string preservation** in `/corporate-memory/admin`. `submitEditItem` was sending `audience=""` for items whose stored audience was NULL, which silently broke visibility (the audience filter checks `audience IS NULL OR audience = 'all'`, neither of which matches empty string). Now empty form values for `audience`/`category`/`domain`/`content` are sent as JSON `null` so the backend stores NULL. (Devin BUG_0001 on PR #141 5f649a4 review.)

## [0.22.0] — 2026-04-30

### Fixed

- **`/api/v2/sample/{table_id}`, `/api/v2/schema/{table_id}`, `/api/v2/scan/estimate`, and `/api/v2/scan` now return structured 502/400 instead of bare 500 when BigQuery raises `Forbidden`/`BadRequest`.** Issue #134. Previously, `_fetch_bq_sample`, `_fetch_bq_schema`, `_bq_dry_run_bytes`, and `_run_bq_scan` had no `try/except`, so a cross-project SA without `serviceusage.services.use` on the data project surfaced as an empty HTTP 500 — operators got no diagnostic. All four call sites now translate `google.api_core.exceptions.Forbidden` to HTTP 502 with `error: "cross_project_forbidden"` (when the message mentions `serviceusage`) plus a `details.hint` pointing at `data_source.bigquery.billing_project` in `instance.yaml`, or `error: "bq_forbidden"` for non-serviceusage ACL denials. `BadRequest` translates to HTTP 400 (`bq_bad_request`) on `/scan/estimate` and `/scan` since their SQL is user-derived (built from `req.select`/`where`/`order_by`), and to HTTP 502 (`bq_upstream_error`) on `/sample` and `/schema` where SQL is server-constructed (server-built `SELECT * … LIMIT n` and `INFORMATION_SCHEMA.COLUMNS` queries respectively). The strict `_fetch_bq_schema` path is wrapped; the best-effort `_fetch_bq_table_options` path retains its existing `try/except → return {}` so `/schema` still returns 200 with empty partition info if BQ metadata is unreachable. `/api/v2/sample` additionally falls back to `data_source.bigquery.billing_project` (with `data_source.bigquery.project` as the default) — `/scan/estimate` and `/scan` already had this fallback.

### Changed

- **BREAKING for deployments using `BIGQUERY_PROJECT` env var alongside `data_source.bigquery.project` in `instance.yaml`.** Issue #134. The env var now sets BOTH billing and data project (used as both the FROM-clause project AND the billing/quota target), overriding `data_source.bigquery.project` for FROM-clause construction in `v2_scan` / `v2_sample` / `v2_schema`. Previously `BIGQUERY_PROJECT` only affected `RemoteQueryEngine` billing and was ignored by the v2 endpoints (which read `instance.yaml` directly). Migrate by clearing `BIGQUERY_PROJECT` and setting `data_source.bigquery.billing_project` + `data_source.bigquery.project` in `instance.yaml` instead — the env var remains as a legacy override only.

### Internal

- **New shared module `connectors/bigquery/access.py` — `BqAccess` facade.** Issue #134. Unifies BigQuery project resolution (`BIGQUERY_PROJECT` env → `instance.yaml billing_project` → `instance.yaml project`), `bigquery.Client` construction, DuckDB-extension session setup (`INSTALL/LOAD/SECRET` from `get_metadata_token()`), and Google-API error translation (`translate_bq_error()` mapping `Forbidden`/`BadRequest`/`GoogleAPICallError` to typed `BqAccessError` with `kind` → HTTP-status mapping). Replaces four near-identical inline blocks across `v2_scan`, `v2_sample`, `v2_schema`, and `RemoteQueryEngine`. FastAPI endpoints inject via `Depends(get_bq_access)` (process-cached; `instance_config.reset_cache()` invalidates this cache too, so admin server-config saves hot-reload BQ project IDs without container restart); `RemoteQueryEngine` injects via `bq_access=BqAccess(...)` constructor kwarg with lazy resolution (DuckDB-only paths never trigger BQ config lookup). When BigQuery isn't configured, `get_bq_access()` returns a sentinel `BqAccess` whose `client()` / `duckdb_session()` raise `BqAccessError(not_configured)` only when actually called — non-BQ instances (Keboola-only, CSV-only) get clean `Depends()` resolution and 200s on local-source v2 requests. Two known-duplicate sites (`connectors/bigquery/extractor.py`, `scripts/duckdb_manager.register_bq_table`) explicitly out of scope; tracked as follow-up.
- **Internal API change in `RemoteQueryEngine`**: `__init__` no longer accepts `_bq_client_factory` (test-only injection point, prefix `_`). Tests migrate to `RemoteQueryEngine(..., bq_access=BqAccess(projects, client_factory=...))`. The `BqAccessError` raised internally by `BqAccess` is translated to the existing `RemoteQueryError(error_type="bq_error")` shape in `_get_bq_client`, preserving the public contract — CLI (`cli/commands/query.py`) and `/api/query/hybrid` callers see no change. Removed the stale docstring at `src/remote_query.py` referencing `scripts.duckdb_manager._create_bq_client` as the default factory (it never was).
- **Side-effect behavior change for unusual cross-project setups in `/api/v2/sample`.** Issue #134. The FROM-clause project for `/sample` is now `data_source.bigquery.project` (the data project) rather than the conflated `billing_project` value — the Phase 1 fix passed `billing_project` (when set) as both the billing target AND the FROM-clause project. Deployments where `billing_project ≠ project` AND the queried table physically lives in `billing_project` (an unusual setup contradicting the documented config semantics) must move the table to the data project or unset `billing_project`. No effect on the standard cross-project setup (table in data project, jobs billed to billing project).
- `scripts/smoke-test.sh`: assertion 8 now hits `/api/admin/registry` (the current admin tables endpoint). The old `/api/admin/tables` URL was renamed long ago and the smoke test was returning 404 on every run — it only surfaced as a deploy failure when the full release pipeline first triggered the rollback path on the post-#137 deploy (run 25151878647). Same stale URL was also fixed in `CLAUDE.md`, `README.md`, and `dev_docs/server.md` — the routes now correctly point at `POST /api/admin/register-table` (create) and `PUT /api/admin/registry/{id}` (update).
- `.github/workflows/release.yml` smoke-test job: added `Log in to GHCR` step. The auto-rollback's `docker push :stable` was hitting `unauthenticated: User cannot be authenticated with the token provided` because the smoke-test job had no GHCR login of its own. Result: a failed deploy left `:stable` pointing at the broken image. The rollback step also got an explicit `GH_TOKEN` env, and the workflow's top-level `permissions` block gained `issues: write`, so its `gh issue create` call actually creates the alert issue (was silently swallowed by the `|| echo` fallback because of both the missing env var AND the missing scope).
## [0.21.0] — 2026-04-30

### Internal

- `scripts/dev/agnes-client-reset.sh` — destructive cleanup of an Agnes *client* install on a developer workstation, mirror image of `app/web/setup_instructions.py` so an onboarding-from-scratch test is reproducible. Removes the `da` CLI (`uv tool uninstall`), `~/.config/da` / `~/.agnes` / `~/.claude/skills/agnes`, the Claude Code `agnes` marketplace + its plugins, the Agnes CA from the OS trust store (Windows `certutil -delstore`, macOS `security delete-certificate -Z`, Linux `update-ca-certificates`/`update-ca-trust`), the `AGNES_CA_PEM_TRUST` block from the user's shell rc (with `.agnes-reset.bak` backup), and `/tmp/agnes*.whl` matches. Cross-platform (Git Bash on Windows / macOS / Linux); `--yes` skips the confirm prompt, `--dry-run` prints actions without executing.

### Changed

- **Trust block heredoc trimmed to 8 lines so reset script's `skip = 8` matches exactly (Devin review round 3 on PR #137).** The `_tls_trust_block` heredoc was emitting 9 lines into the user's shell rc (a leading empty line + the `AGNES_CA_PEM_TRUST` marker + 7 export/comment lines), but `scripts/dev/agnes-client-reset.sh` awk strips exactly 8 lines starting at the marker — leaving the leading empty line behind. On repeated install/reset cycles, those stray empty lines accumulated in `~/.zshrc` / `~/.bashrc`. Removed the leading empty string from the heredoc body in `_tls_trust_block` so the heredoc now writes exactly 8 lines, matching the awk count. Added two regression tests that pin the invariant — one asserts the heredoc body length, the other parses `skip = N` out of the reset script via regex and cross-checks it against the heredoc body line count, so future drift on either side fails loudly.

- **Marketplace block re-detects `$PLATFORM` so Linux actually gets the direct-HTTPS attempt (Devin review round 2 on PR #137).** `$PLATFORM` is set in step 0(a) but the prompt itself warns that env vars don't persist across separate Bash invocations (step 0(e) IMPORTANT note). The marketplace step's `case "$PLATFORM" in` ran in a later Bash call where `$PLATFORM=""`, falling through to the `*)` catch-all which hard-codes `MARKETPLACE_VIA=clone` — defeating the Linux-only direct-HTTPS attempt that node-based `claude` would have honored via `NODE_EXTRA_CA_CERTS`. Marketplace block now re-detects `$PLATFORM` via the same `uname -s` switch from step 0(a) before its case statement, making the block self-contained. Same fix not applied to step 0(c)'s `$PLATFORM` use because step 0 is meant to run as a single Bash block (a→b→c→d→e in sequence) where the variable is still in scope.

- **Setup prompt no longer references steps that may not have been emitted (Devin review on PR #137).** Three places hard-coded references to optional steps regardless of whether those steps were actually rendered. (1) Confirm step's summary bullets unconditionally listed "Which CA bundle source got picked in step 0(d)" and "Whether the marketplace add went via direct HTTPS or via the git-clone fallback" — both phantom in the default no-CA, no-plugins flow, and an LLM following the prompt would either ask the user about non-existent steps or hallucinate. `_FINALE_LINES_TEMPLATE` constant replaced with `_finale_lines(has_ca, has_marketplace)` that conditionally appends each bullet. (2) Preamble's "The fallback chain inside step 0(d) is documented and OK to use" pointed at a non-existent step when `ca_pem` was unset. `_preamble_lines(has_ca)` now drops that line in the no-trust-block path; the "don't disable TLS verification" guidance stays unconditional (valid generic advice). (3) Trust block step 0(c) said "without this, step 7's marketplace add fails" — stale after the layout reordering moved marketplace to step 5 (and made it optional). Reworded to describe the consequence without naming a step number.

- **Marketplace step now uses the git-clone fallback on macOS too — not only Windows — and strips the PAT from the cloned repo's `.git/config` after clone.** First fix: `claude` on macOS arm64 ships as a Mach-O binary with a `__BUN` segment (single-file `bun build --compile`); reverse engineering its `strings` table shows it recognizes `NODE_EXTRA_CA_CERTS` / `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` / `CURL_CA_BUNDLE` (including a "NODE_EXTRA_CA_CERTS detected" log line) but in practice none of them — nor the macOS login keychain — is honored for the marketplace HTTPS request, leaving the direct-add path failing with `unable to verify the first certificate` even after step 0(c) registered the cert. So the marketplace `case` now matches `linux)` for the direct-then-fallback path and `*)` (= Windows + macOS, both Bun-compiled) for straight-to-clone. Second fix: `git clone https://x:<PAT>@host/...` writes the URL verbatim into `~/.agnes/marketplace/.git/config`, so the PAT then sits in plaintext at a path that gets read by cloud-sync agents (iCloud, OneDrive) and antivirus scanners on default home setups; after clone we now run `git remote set-url origin "https://<host>/marketplace.git/"` to drop the token, plus a best-effort `chmod 700`/`chmod 600` (wrapped in `|| true` so it's a no-op on Windows NTFS via MSYS / Git Bash). Marketplace registration uses the local FS path, not the remote URL, so removing the token doesn't break anything — refreshes go via re-running setup with a fresh PAT from the dashboard. Third fix: each shell-out (`git clone`, `claude plugin marketplace add`, `claude plugin install <name>@agnes`) is now wrapped in `|| { echo "ERROR..." >&2; exit 1; }` so a failure halts the prompt loudly instead of falling through to a confusing downstream error (e.g. failed clone → `marketplace 'agnes' not found` from the next `plugin install`). Fourth fix: the diagnose step now calls out that `db_schema: unknown` is also normal for non-admin roles (e.g. `analyst`) on populated instances, not just on fresh installs — analyst lacks grants on the system schema, so the field stays `unknown` forever and was previously misread as a yellow check.

- **Setup prompt step ordering reshuffled so all installation work runs before the human-loop skills question.** Old order interleaved the human question (skills, step 5) between install (step 1) and marketplace/plugins (step 7), which led the assistant to either block on the user mid-install or "do the rest in parallel" while waiting. New order: install → login → verify → git check → marketplace + plugins → diagnose → **skills (last interactive step before Confirm)** → Confirm. With marketplace plugins to install, that's steps 1-2-3-4-5-6-7-8; without plugins, 4-5 (git/marketplace) collapse out and diagnose/skills/confirm renumber to 4-5-6. The skills step now explicitly tells the assistant to *wait* for the user's answer before moving to Confirm — the old "you can continue in parallel" hint is gone because there's no longer anything to do in parallel. `da diagnose` running late doubles as a final smoke test after plugins are in place.

- **Setup prompt's TLS trust block rewritten to be cross-platform and to dodge three TLS pitfalls observed across real workstation setups.** The previous block exported `SSL_CERT_FILE`/`NODE_EXTRA_CA_CERTS`/`GIT_SSL_CAINFO` all pointing at the single Agnes CA; this caused (1) every Python tool in the same shell to lose its system trust store (PyPI immediately broke with `UnknownIssuer` because `SSL_CERT_FILE` is a *replace*, not an append), (2) `uv tool install <https-url>` against the Agnes wheel endpoint to fail with rustls' `CaUsedAsEndEntity` because the Agnes leaf cert is its own CA — `--native-tls` doesn't help (the rejection happens during chain validation, not trust lookup), and (3) `claude plugin marketplace add` to fail on Windows because `claude.exe` ignores both the OS trust store and `NODE_EXTRA_CA_CERTS` for marketplace HTTPS. The new step 0 (a) detects platform via `uname` + `$SHELL` and picks the correct shell rc file (zsh→`.zshrc`, bash on macOS→`.bash_profile`, else→`.bashrc`), (b) writes the cert PEM via single-quoted heredoc, (c) registers the cert in the OS trust store (Windows `certutil -user -addstore Root`, macOS `security add-trusted-cert`, Linux `update-ca-certificates`/`update-ca-trust`) — no admin rights needed, idempotent on re-run — so native binaries that bypass our env vars still trust the host, (d) builds a *combined* CA bundle at `~/.agnes/ca-bundle.pem` (system roots + Agnes CA) using a fallback chain for the system roots source (system `python3 -c 'import certifi'` → distro/curl bundle paths → `uv run --with certifi` as last resort), (e) persists `SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE`/`GIT_SSL_CAINFO` pointing at the *combined* bundle while keeping `NODE_EXTRA_CA_CERTS` on just `ca.pem` (Node's append semantics). When the trust block is emitted, step 1 also switches to a curl-then-local-install pattern (`curl --cacert` to download the wheel, `uv tool install --native-tls --force <local-file>` to install) so rustls never sees the Agnes host. Step 7 (marketplace) goes platform-aware: Windows skips the direct HTTPS attempt and uses a system `git clone` fallback (system git honors `GIT_SSL_CAINFO`), macOS/Linux try direct first. Step 4 (diagnose) calls out that `db_schema: unknown` and `data: 0 tables` are normal on fresh installs. Step 5 (skills) makes clear the assistant can continue with steps 6-7 while waiting for the user's skills answer. Step 12 (marketplace) calls out the harmless `git: 'credential-manager-core' is not a git command` warning so the operator doesn't chase it. The legacy `git config sslVerify=false` downgrade path stays as a fallback for instances without a `fullchain.pem` on disk (so existing `AGNES_DEBUG_AUTH` setups keep working).

### Added

- **"Set up a new Claude Code" prompt now bootstraps the marketplace and plugins.** The clipboard payload generated by the dashboard CTA appends a git pre-flight check (`git --version`, with `brew install git` / `winget install --id Git.Git` install commands for macOS / Windows) followed by a marketplace-registration step that runs `claude plugin marketplace add "https://x:<PAT>@<host>/marketplace.git/"` and one `claude plugin install <plugin>@agnes --scope project` per RBAC-allowed plugin (resolved via `marketplace_filter.resolve_allowed_plugins`). When the user has no plugin grants, the original 6-step layout is preserved. When `AGNES_DEBUG_AUTH` is enabled on the server (dev/self-signed-cert instances), a host-scoped `git config --global http."<server>/".sslVerify false` line is also included so the marketplace clone works against the self-signed endpoint. Plugins load on the next `claude` start.
- **Setup prompt inlines the server's TLS cert as a step-0 trust block on instances with a private CA / self-signed chain.** `app.web.router._read_agnes_ca_pem` reads `/data/state/certs/fullchain.pem` (path overridable via `AGNES_TLS_FULLCHAIN_PATH`; the file is bind-mounted into the app container by `docker-compose.host-mount.yml` from the same location `agnes-tls-rotate.sh` writes). Self-signed leaves and CA-signed leaves whose issuer isn't in the server-side `certifi` trust store are inlined into the prompt; publicly-trusted chains (Let's Encrypt etc.) are skipped so users don't unnecessarily narrow their default Python TLS trust. The inlined block writes the PEM to `~/.agnes/ca.pem` via single-quoted heredoc (so `$`/backtick chars in the cert never shell-expand) and exports `SSL_CERT_FILE`, `NODE_EXTRA_CA_CERTS`, `GIT_SSL_CAINFO` for the current shell + persists them to `~/.bashrc`/`~/.zshrc` (idempotent via a marker grep guard) so `da` keeps trusting the host across new terminal sessions. When the trust block is emitted, the legacy `git config sslVerify=false` downgrade is suppressed — full TLS validation re-enabled, just against the inlined cert. Cross-platform (macOS bash/zsh + Windows Git Bash) — same env vars, same heredoc syntax. Replaces the `git config sslVerify=false`-only path that broke `claude plugin marketplace add` (Node has its own HTTPS client and ignores `git config`) and `uv tool install` (rustls, no insecure flag) on self-signed instances.

## [0.20.0] — 2026-04-29

### Added
- Dev debug toolbar gated by `DEBUG=1`. Mounts `fastapi-debug-toolbar` with panels
  for headers, routes, settings, versions, timer, logging, and a custom DuckDB
  panel that captures every `con.execute()` from `src/db.py` (tagged by
  `system` / `analytics` / `analytics_ro`). See `docs/development.md`.
- `X-Request-ID` request header / response header on every FastAPI response, plus
  a `request_id` field in JSON logs for cross-process correlation.
- Request-ID surfaced end-to-end on error responses: `Reference: <rid>` block on
  the rendered `error.html` page (with `user-select: all` for one-click copy)
  and a `"request_id": "<rid>"` field in the JSON 5xx body. The same id appears
  in the `x-request-id` response header, so a support ticket can be traced from
  a single value the user sees on the page.
- Dev log lines now carry the request id via `_RequestIdFilter` — `RichHandler`
  format is `[<rid>] [<logger>] <msg>` (or `[-]` outside of a request scope).
  JSON formatter already included `request_id`; this closes the gap for
  `DEBUG=1` development.
- Centralized `app.logging_config.setup_logging()` — replaces 23 scattered
  `logging.basicConfig(...)` calls. Uses `rich.logging.RichHandler` in dev
  (`DEBUG=1`) and JSON to stderr in prod.

### Changed
- All service entrypoints (`services/scheduler/__main__.py`, `ws_gateway`,
  `telegram_bot`, `corporate_memory`, `session_collector`, `verification_detector`)
  and CLI scripts under `scripts/` and `connectors/jira/scripts/` now call
  `setup_logging(__name__)` instead of inline `basicConfig`. Library modules no
  longer configure root logger at import time.
- **BREAKING** Telegram bot no longer writes to `/data/notifications/bot.log`.
  All bot logs go to stdout, captured by Docker. Use
  `docker compose logs -f notify-bot` to read them. Operators tail-ing the file
  must update their runbooks; see `dev_docs/telegram_bot.md` for the new
  procedure (including `journalctl` fallback for non-Docker hosts).
- Toolbar middleware is mounted INSIDE the GZip middleware (innermost on
  response) so the toolbar can decode HTML before compression. RequestIdMiddleware
  remains outermost; production behavior (DEBUG unset) is byte-identical to
  before.

### Fixed
- `scripts/ops/agnes-auto-upgrade.sh`: fail-fast guard before any `docker
  compose` action — when the VM has a config disk attached
  (`/dev/disk/by-id/google-config-disk` exists), `/data/state` MUST be backed
  by it. Three retry attempts with backoff, then exit non-zero. Prevents the
  silent regression where docker host-mount propagation unmounts the config
  disk and the app writes user state (DuckDB, marketplaces, session secret)
  onto `/data` (sdb) — wiped on the next container recreate. Re-applies
  `mount --make-rprivate /data /data/state` on every run to defend against
  propagation regressions.
- Removed rogue module-level `logging.basicConfig` from `app/api/sync.py` that
  was reconfiguring root logger every time the api module was imported.
- `RequestIdMiddleware` rewritten as a pure ASGI middleware (was
  `BaseHTTPMiddleware`). Removes the early `request_id_var.reset` in `finally`
  that fired BEFORE BackgroundTasks ran, causing them to lose the id. Also
  side-steps the known `BaseHTTPMiddleware` ContextVar-cross-task issue.
- Incoming `X-Request-ID` headers are now sanitized (alnum + `-` / `_`,
  truncated to 64 chars; falls back to a fresh uuid if nothing legal remains).
  Closes a CRLF log-forging vector when log handlers don't escape newlines.
- `_wants_html` no longer treats `Accept: */*` (curl default) or empty Accept
  as "wants HTML". Operators who curl non-API paths get JSON `{"detail": "..."}`
  as before — only real browsers (with `Accept: text/html,...`) get HTML error
  pages. (Devin ANALYSIS_0003 on PR #136 review.)
- Subprocess extractor in `app/api/sync.py` re-installs `logging.basicConfig`
  so INFO-level extraction progress from `connectors.keboola.extractor.run()`
  reaches stderr again (was silently dropped by Python's `lastResort` handler
  after the import-time `basicConfig` cleanup). (Devin BUG_0002.)
- `.env.template` comment for `DEBUG=1` no longer claims to enable
  `FastAPI debug=True` — that flag is intentionally NOT toggled (Starlette's
  `ServerErrorMiddleware` would intercept unhandled exceptions before the
  custom error handler runs). (Devin BUG_0001.)
- **Security**: HTML error page (500) no longer leaks `str(exc)` in
  production. The JSON branch already guarded that string behind `debug_on`
  but the HTML branch did not — browser users could see raw exception
  messages containing DB paths, SQL fragments, internal hostnames, or
  credentials embedded in connection strings. The HTML branch now mirrors
  the JSON branch's `debug_on` check; production users see only
  `"Internal server error"` plus the request id. (Devin BUG on b1c6ee9 review.)

### Internal
- `pyproject.toml`: added `fastapi-debug-toolbar>=0.6.3` to dev optional deps.
- `services/telegram_bot/config.py`: removed unused `BOT_LOG_FILE` constant.
- `tests/conftest.py`: removed stale comment about bot.py FileHandler.

## [0.19.0] — 2026-04-29

### Added
- `table_registry.sync_schedule` is now honored at runtime. `POST /api/sync/trigger` (called by the scheduler sidecar every 15 min by default) drops local tables whose schedule says they are not due. Tables without a schedule continue to sync on every tick (opt-in feature). Manual `POST /api/sync/trigger {"tables":[...]}` bypasses the schedule filter — operator override always wins. (#79)
- `script_registry.schedule` is now honored at runtime via the new endpoint `POST /api/scripts/run-due` (admin-only). The scheduler sidecar fires this every 60 s by default. Each due script is claimed atomically (`last_status='running'`), executed in a BackgroundTask, and the outcome written to `last_run` / `last_status`. Scripts already in `running` state are skipped — no concurrent runs of the same script. (#78)
- Four new env vars on the scheduler sidecar: `SCHEDULER_DATA_REFRESH_INTERVAL`, `SCHEDULER_HEALTH_CHECK_INTERVAL`, `SCHEDULER_SCRIPT_RUN_INTERVAL`, `SCHEDULER_TICK_SECONDS`. All accept positive integers (seconds); tick must be ≤ smallest job interval. Documented in `docs/DEPLOYMENT.md` → Scheduler tuning. (#77)
- `RegisterTableRequest.sync_schedule`, `UpdateTableRequest.sync_schedule`, and `DeployScriptRequest.schedule` now reject malformed strings with a Pydantic 422 (e.g. `"hourly"`, `"daily 25:00"`). The accepted forms are unchanged: `every Nm`, `every Nh`, `daily HH:MM[,HH:MM,...]`. **Note**: cron expressions (`"0 8 * * MON"` etc.) were never honoured by the runtime evaluator — they used to round-trip through the API as a silent no-op, and now they get a loud 422 at register/deploy time. Operators using cron strings must convert to one of the supported forms. (#78, #79)
- New `verify_ssl` knob in the `openmetadata:` section of `instance.yaml` (default `true`). Operators on internal CAs / self-signed catalogs must set it explicitly. (#89)

### Changed
- **BREAKING** `POST /api/scripts/deploy` now validates the source against the safety blocklist BEFORE persisting (previously safety checks ran only at execution time). Scripts containing blocked imports / patterns return 400 from `/deploy` instead of being stored and failing every scheduler tick. Closes the claim-fail-retry loop where the new `/api/scripts/run-due` endpoint would re-claim and re-fail an unrunnable deployed script every minute. (#78)
- **BREAKING** `OpenMetadataClient` now defaults to `verify=True` for TLS. The previous version hardcoded `verify=False` and suppressed urllib3's "Unverified HTTPS request" warning at import time (which leaked to every other httpx client in the process). Existing deployments on self-signed certificates without an explicit opt-out will start failing TLS verification — set `verify_ssl: false` in the `openmetadata:` block of `instance.yaml`, or supply a CA bundle path, before upgrading. Both production call sites (`connectors/openmetadata/enricher.py`, `src/catalog_export.py`) read the new `verify_ssl` config knob and pass it through. (#89)
- **BREAKING** `GET /marketplace/info` (admin-only debug endpoint) `name` field now returns the plugin's authoritative name from its `plugin.json` (e.g. `plug-x`) instead of the slug-prefixed form (`<slug>-<plug-x>`). The slug-prefixed form moved to a new `prefixed_name` field next to it; `original_name` is unchanged. Side-effect of the `/plugin` UI fix below — the synth marketplace.json's `name` field had to switch over for Claude Code's catalog lookup to work, and `/marketplace/info` mirrors that surface for consistency. Any downstream tooling that parsed the `name` field expecting the slug-prefixed format must now read `prefixed_name`. (#133)

### Fixed
- **`/plugin` UI in Claude Code rendered "Plugin <X> not found in marketplace" in the Components panel** for every plugin Agnes served, even though agents/skills/commands loaded correctly under the plugin's own namespace. Root cause: the synthetic `.claude-plugin/marketplace.json` listed each plugin under a slug-prefixed `name` (`<slug>-<plugin>`) while the plugin's authoritative `.claude-plugin/plugin.json` kept the original name. Claude Code resolves the loaded plugin back to its catalog entry by `plugin.json` name, so the lookup missed every entry. The synth manifest now reads the plugin's authoritative name from `<plugin_dir>/.claude-plugin/plugin.json` (falling back to the upstream marketplace.json's `name` when the plugin manifest is absent or unreadable). The directory layout under `plugins/<slug>-<plugin>/...` keeps the prefix so two upstream marketplaces that ship a same-named plugin still get distinct on-disk paths in the ZIP / git tree — their catalog entries will then collide under the same `name`, which is the correct surface (admin RBAC decides which upstream wins, same as if a user added both upstream marketplaces directly to Claude Code). `/marketplace/info` now exposes `prefixed_name` alongside `name` so operators can still disambiguate cross-marketplace shadowing. (#133)

### Internal
- `src/scheduler.py` now exports `is_valid_schedule(s)` and `filter_due_tables(configs, sync_state_repo)` for reuse across the sync filter, the script runner, and Pydantic validators.
- `ScriptRepository` gains `claim_for_run(script_id)` and `record_run_result(script_id, status)` — the atomic primitives for the scheduled-script execution path. `claim_for_run` uses `UPDATE … WHERE last_status IS DISTINCT FROM 'running' RETURNING id` for race-free claim.
- `services/scheduler/__main__.py` JOBS list refactored to a `build_jobs()` factory that reads + validates env at startup.

### Known limitations
- **Stuck `last_status='running'`**: a scheduled script whose BackgroundTask crashes mid-run (process killed, OOM, gateway timeout) stays claimed forever. Recovery: `UPDATE script_registry SET last_status = NULL WHERE id = ?` from a DuckDB shell. Auto-recovery via max-runtime detection is intentionally out of scope for v0.19.0; revisit if it bites in practice.
- **Schedule quantization rounds up**: `SCHEDULER_*_INTERVAL` accepts seconds but the underlying schedule grammar is minute-grained. Non-multiples of 60 round UP to the next minute (90 s → `every 2m`, never `every 1m`) so a job never fires more often than the operator configured. Sub-minute values clamp to `every 1m`. Documented in `docs/DEPLOYMENT.md` → Scheduler tuning.

## [0.18.0] — 2026-04-29

### Added

- **Corporate-memory tree view + cross-axis filtering** on `/corporate-memory` and `/corporate-memory/admin`. Operators choose a grouping axis (domain / category / tag / audience) and combine it with chip filters (status, source_type, audience, has-duplicate-hint, search). Tree uses native `<details>`; localStorage persists open/closed state per axis; no new dependencies. Issue #62.
- **Corporate-memory duplicate-candidate hints** — admin sees a "Duplicate Candidates" tab with likely-duplicate item pairs detected by entity overlap (Jaccard score, ≥2 shared entities, same domain). Resolution actions: `duplicate` / `different` / `dismissed`. Auto-merge intentionally not included. Issue #62.
- **Bulk-edit endpoints**: `PATCH /api/memory/admin/{id}` (now accepts category/domain/tags/audience/title/content, was title+content only); `POST /api/memory/admin/bulk-update` for multi-id mutations with per-item audit rows; new "Move to category / domain / audience" + "Add/Remove tag" actions in the admin batch bar. Issue #62.
- **`GET /api/memory/stats`** now includes `by_tag` (DuckDB `json_each` over tags) and `by_audience` aggregations to power chip-filter pickers. Issue #62.
- **`GET /api/memory/tree?axis=...&...`** — server-side grouping endpoint that returns `{groups: [{key, label, count, items: [...]}]}`, RBAC-filtered, with chip filters (`status_filter`, `source_type`, `audience`, `q`, `has_duplicate`). Issue #62.
- **`da admin memory {tree,edit,bulk-edit,stats,duplicates list,duplicates resolve}`** — full CLI parity for the new admin endpoints. Issue #62.
- **Schema v17**: new `knowledge_item_relations` table for duplicate-candidate hints. PK `(item_a_id, item_b_id, relation_type)` with canonical `(min, max)` pair ordering at the repository layer; auto-migration v16→v17 idempotent. Issue #62.
- **BigQuery table registration via admin UI + CLI (issue #108 — Milestone 1).** Operators on a BigQuery instance can now register a BQ table or view as a remote DuckDB master view from `/admin/tables` or `da admin register-table`, without hand-editing `table_registry` or running the extractor by hand. The register modal branches on `data_source.type` server-side: BQ instances see Dataset / Source Table / View Name / Description / Folder / Sync Schedule; Keboola instances keep the discovery-driven flow. Submit runs `/api/admin/register-table/precheck` first (round-trips `bigquery.Client.get_table` to confirm the table exists and the SA can see it; surfaces row count + size + column count in the modal), then commits. The server validates BQ-specific shape (dataset / source_table / DuckDB-safe identifiers / GCP project_id grammar), forces `query_mode=remote` + `profile_after_sync=false`, and synchronously rebuilds `extract.duckdb` + master views with a 5s wall-clock budget — on overrun, the rebuild continues in a `BackgroundTask` and the API returns 202 with `{"status": "accepted", "view_name": ...}` instead of 200. View-name collisions (distinct from id collisions) return 409 to stop two callers from registering the same DuckDB view via different display names. `sync_schedule` is accepted and stored but not yet evaluated by the scheduler — see issue #79; addressed in Milestone 3 of #108. See `docs/DATA_SOURCES.md`.
- `POST /api/admin/register-table/precheck` — validation-only sibling of register-table. Returns `{"ok": true, "table": {rows, size_bytes, columns, …}}` for BQ rows after round-tripping `get_table`; surfaces NotFound → 404, Forbidden → 403, anything else → 400 with the GCP error verbatim. Also runs Pydantic validation for non-BQ source types so the CLI / UI gets a single endpoint shape.
- `--dry-run` flag on `da admin register-table` — calls `/precheck` and pretty-prints rows / bytes / columns; exits 0 on `ok`, 1 on validation or source-side error.
- Audit-log entries on every `register_table` / `update_table` / `unregister_table` mutation — closes the asymmetry where instance-config saves audited but registry mutations didn't (Decision 4 in #108). Secret-named fields in the request payload are masked as `***`; `description` is logged raw.
- **Google Workspace group prefix filter + system-group mapping.** Three new env vars wire the OAuth callback's group sync to a configurable Workspace prefix and route the admin/everyone Workspace groups into the seeded system rows.
  - `AGNES_GOOGLE_GROUP_PREFIX` — when set (e.g. `grp_acme_`), only Workspace groups whose email local part starts with the prefix are mirrored into `user_group_members`. Empty = legacy behavior (mirror every fetched group).
  - `AGNES_GROUP_ADMIN_EMAIL` — Workspace group email that maps onto the seeded `Admin` system row instead of creating a fresh `user_groups` entry. Members of that Workspace group land in `Admin` directly.
  - `AGNES_GROUP_EVERYONE_EMAIL` — same mechanism for `Everyone`.
- **Login gate.** When `AGNES_GOOGLE_GROUP_PREFIX` is set and the user's Workspace fetch returned a non-empty list with zero prefix matches, the callback redirects to `/login?error=not_in_allowed_group` with a friendly inline banner. Empty fetch results (transient Cloud Identity failures) preserve the cached membership and let the login proceed — fail-soft only the soft-fail path; an explicit no-match still blocks. New error code `group_check_unavailable` is wired through the login banner for future use.
- **Admin UI subtitle for synced groups.** The `/admin/groups` table and the `/admin/groups/{id}` detail page render a derived display name (prefix stripped, `@domain` removed, capitalized) above a small monospace subtitle showing the full Workspace email. Edit / Delete affordances are hidden on Google-managed rows, and a "managed by Google Workspace — read-only here" banner appears on the detail page.

### Changed

- **BREAKING** Auto-`Everyone` membership for new users was removed. `UserRepository.create` no longer writes a `user_group_members` row, and `app.auth.access._user_group_ids` no longer adds a virtual `Everyone` id to the result. Every membership now traces to a real source row (`admin`, `google_sync`, or an explicit `system_seed`). If you relied on the implicit-Everyone behavior for plugin visibility, grant the plugin to a real group (e.g. an `everyone@example.com` Workspace group mapped via `AGNES_GROUP_EVERYONE_EMAIL`).
- **Admin UI / API are read-only on Google-managed groups.** `created_by='system:google-sync'` rows, plus the seeded `Admin` / `Everyone` rows when the matching email-mapping env var is set, return `409` with body `{"detail": {"code": "google_managed_readonly", ...}}` from `PATCH /api/admin/groups/{id}`, `DELETE /api/admin/groups/{id}`, `POST /api/admin/groups/{id}/members`, `DELETE /api/admin/groups/{id}/members/{user_id}`, `POST /api/admin/users/{id}/memberships`, `DELETE /api/admin/users/{id}/memberships/{group_id}`. Edit through admin.google.com, then sign in again to refresh.
- **Audit action names for corporate-memory operations renamed** from `km_<action>` to `corporate_memory.<action>` to match the 0.15.0 CHANGELOG documentation. The audit-tab filter accepts both prefixes for back-compat with rows already in the audit log (no historical-row rewrite). Issue #62.
- **`onDomainChange()` UX bug fixed** on `/corporate-memory`: domain and category filters now compose instead of resetting each other when either changes. Issue #62.
- `POST /api/memory/admin/edit` continues to accept title/content as before; the new `PATCH /api/memory/admin/{id}` is the recommended path for everything else (including title/content). The legacy endpoint is kept one release for back-compat.

### Internal

- New env vars surfaced into `ConfigProxy` so templates can derive the friendly display name client-side.
- New `is_google_managed: bool` field on `GroupResponse` (the API surface for the admin UI's group list/detail).
- New `UserGroupMembersRepository.has_any_google_sync_membership` helper (currently diagnostic; kept for a future tightening of the gate).
- New tests in `tests/test_google_group_prefix_sync.py`; `tests/test_repositories.py::TestUserRepositoryEveryoneAutoMember` renamed to `TestUserRepositoryNoAutoMembership` with inverted assertion; two `tests/test_marketplace_filter.py` tests adapted to the no-implicit-Everyone semantics. See `docs/auth-groups.md` for the full reference.

### Fixed

- `PATCH /api/memory/admin/{id}` now switches from `model_dump(exclude_none=True)` to `exclude_unset=True`, so an explicit `null` in the request body clears the field (e.g. `{"audience": null}` resets a previously-set audience to NULL). Pre-fix nulls were silently dropped, leaving no path to clear `audience` and only the empty-string short-circuit for `domain`. The endpoint now distinguishes "field absent from body" (untouched) from "field explicitly set to null" (cleared). Both `PATCH /api/memory/admin/{id}` and `POST /api/memory/admin/bulk-update` now reject an explicit `null` for `title` (NOT NULL in the schema) at the boundary with HTTP 400 instead of bubbling up as a 500 (PATCH) or per-item Constraint Error (bulk). Issue #62 / PR #126 review.
- Empty-string `domain` is now consistently allowed across `POST /api/memory`, `PATCH /api/memory/admin/{id}`, and `POST /api/memory/admin/bulk-update` — previously create allowed it (short-circuit on falsy) but PATCH/bulk-update rejected it with 400, which made it impossible to clear a domain through the admin endpoints. Issue #62 / PR #126 review.
- Bulk-edit modal `(unset)` option for the domain field is now actually submittable. Pre-fix the JS rejected empty values with "Please enter a value" before the request fired, so the operator-visible "(unset)" option couldn't ever clear a domain even though the backend supports it. Issue #62 / PR #126 review.
- **`POST /api/memory/admin/bulk-update` now enforces an API-layer allowlist** of mutable fields (`category`, `domain`, `tags`, `tags_add`, `tags_remove`, `audience`, `title`, `content`). Pre-fix the endpoint forwarded any key in the repo's `_UPDATABLE_FIELDS` set, which included `status`, `sensitivity`, `is_personal`, and `confidence` — an admin could `POST {"updates": {"status": "mandatory"}}` and silently bypass `/admin/mandate`'s dedicated audit trail (the bulk audit row also stamped `updated_fields: []` for those mutations, leaving no trace of what changed). Disallowed keys now return HTTP 400 with the offending list; the repo layer is unchanged so the per-item `repo.update` path keeps its broader access. Issue #62 / PR #126 review.
- **Tree endpoint `audience=all` chip now includes NULL-audience items**, matching the SQL audience filter (`audience IS NULL OR audience = 'all'`), `count_by_audience` (COALESCE→'all'), and `_bucket_key` (NULL → "all"). Pre-fix the in-memory chip filter compared `item.audience != 'all'` and dropped NULL-audience items from the bucket they were supposed to land in. Issue #62 / PR #126 review.
- **`GET /api/memory/admin/audit` honors `page`** — the SQL had `LIMIT` only and silently returned the first page for every page parameter. Both branches (`action`-filtered and unfiltered) now apply `OFFSET (page - 1) * per_page`. Issue #62 / PR #126 review.
- **`POST /api/memory` validates `domain`** against the same allowlist `PATCH /admin/{id}` and `POST /admin/bulk-update` use, so an item can't be created with a domain it can't later be patched to. Empty / missing domain is still accepted. Issue #62 / PR #126 review.
- **`da admin memory edit --add-tag/--remove-tag` could silently drop existing tags** when the target item lived past page 1 of `/api/memory`. The CLI did GET-then-PATCH for tag mutations, looked the item up in the first 50 rows of the unfiltered list, and overwrote the tag set with `[just_added]` when it didn't find the row. Tag mutations now route through `POST /api/memory/admin/bulk-update` (single-id array, server-side merge with the existing tags). Issue #62.
- **`da admin memory duplicates list` couldn't list both resolution states** — the CLI always sent `resolved=true|false` and the API defaulted to `resolved=false` when omitted, so neither path returned the full set. `GET /api/memory/admin/duplicate-candidates` now treats omitted `resolved` as "no filter"; the CLI omits the flag by default and only sets it when the user passes `--resolved` or `--unresolved`. The web UI continues to pass `resolved=false` explicitly so the actionable backlog stays the default surface. Issue #62.
- `PUT /api/admin/registry/{id}` now preserves the original `registered_at` timestamp instead of resetting it to `now()` on every edit. `TableRegistryRepository.register` accepts `registered_at` as an optional kwarg; `update_table` re-passes the existing value from the row it just read. Closes #130.

## [0.17.0] — 2026-04-29

### Added

- **Shared-secret auth path for the in-cluster scheduler service** (`SCHEDULER_API_TOKEN`). Both the `app` and `scheduler` containers source the same `/opt/agnes/.env` via Docker Compose `env_file:`, so a 256-bit secret generated once at VM provisioning serves both sides symmetrically. The app validates incoming `Authorization: Bearer <secret>` against the env var (constant-time compare; minimum length 32 chars; rejected when env is empty) and resolves matches to a synthetic `scheduler@system.local` user that is a member of the `Admin` system group — every existing RBAC gate (`require_admin`, `require_resource_access`) works unchanged. Audit-log entries from the scheduler are attributed to this user. Rotation: edit `.env`, `docker compose restart app scheduler`. See `app/auth/scheduler_token.py` for the threat model.
- **`POST /api/marketplaces/sync-all`** — admin-only endpoint that runs `src.marketplace.sync_marketplaces()` inside the app process. Wired up so the scheduler container can drive the nightly refresh over HTTP without opening `system.duckdb` directly.

### Fixed

- **Scheduler `marketplaces` job 500-ed every cron tick with `IO Error: Could not set lock on file system.duckdb` after v0.12.1.** The previous implementation called `src.marketplace.sync_marketplaces()` in-process from the scheduler container, but DuckDB permits only one writer per file across processes — the scheduler raced the app's long-lived handle. Switched the job to `POST /api/marketplaces/sync-all`, making the app the sole writer; the scheduler is now a pure cron clock.
- **Scheduler `data-refresh` job 401-ed every 15 minutes** with `Missing or invalid Authorization header` because `SCHEDULER_API_TOKEN` was never propagated by `infra/modules/customer-instance/startup-script.sh.tpl`. The startup script now generates a 64-hex-char secret on first boot via `openssl rand -hex 32`, persists it across reboots by reading back from an existing `.env` (rotation requires explicit operator action — both containers must restart together), and writes it into `/opt/agnes/.env` alongside the other secrets. `app/main.py` seeds the matching synthetic user at startup so the very first cron tick has a valid actor to attribute audit-log entries to. Existing VMs need a one-time `sudo /opt/agnes/agnes-rotate-scheduler-token.sh` (or simply re-run the startup script via `terraform apply -replace='module.agnes.google_compute_instance.vm["<vm-name>"]'`); see migration note in this changelog or rerun the startup script manually.
- **Non-root container couldn't write to host-bind-mounted `/data` after the v0.12.1 USER-agnes flip.** `infra/modules/customer-instance/startup-script.sh.tpl` now `chown -R 999:999 /data` after creating the persistent-disk subdirs (`state`, `analytics`, `extracts`). Without this, a freshly-attached PD is root-owned by default and `USER agnes` (uid 999) cannot open `/data/state/system.duckdb` for write — every authed request 500s with `IOException: Cannot open file ... Permission denied` while `/api/health` (which doesn't open the system DB) keeps returning 200, masking the failure from health-only monitoring. Regression first observed on `agnes-development` on 2026-04-29 after the auto-upgrade picked up `:stable` from the 0.12.1 release. **Existing VMs with PD-backed `/data` need a one-time host-side `sudo chown -R 999:999 /var/lib/docker/volumes/agnes_data/_data && sudo docker restart agnes-app-1 agnes-scheduler-1` to recover** — Terraform `metadata_startup_script` only runs on boot, so an apply alone does not retro-fix running VMs.
- `Dockerfile` pins the `agnes` user to `uid:gid 999:999` explicitly (`useradd --uid 999`). Previously the uid was whatever Debian's `useradd --system` assigned next — happened to be 999 today, but a future base-image change picking 998 or 1000 would silently desync from the startup-script's `chown 999:999`, reintroducing the same incident. Pinning makes the contract grep-able from both sides.
- `scripts/smoke-test.sh` no longer silently SKIPs every authed check when `bootstrap` returns 403 (users exist) and `SMOKE_TOKEN` is not set — it now FAILs loudly. Also adds an unauthenticated DB-touching probe (`POST /auth/email/request`) before bootstrap, since `/api/health` deliberately doesn't open `system.duckdb` (kept cheap for LB probes) and so cannot detect filesystem/permission issues. The new probe catches a class of regression that bypasses health-only monitoring even on instances where bootstrap is closed.
- Corporate memory pages (`/corporate-memory`, `/corporate-memory/admin`) now render the shared app header at full viewport width, matching the dashboard. Previously the `_app_header.html` include sat inside `.container-memory` (max-width: 1000px) and was cropped on wide viewports.
- `release.yml` now publishes a `:dev-<slug>` + `:dev-<prefix>-latest` image when a fresh branch is pushed off `main` with no extra commits. Pre-fix, `paths-ignore` on the `push` event diffed the new ref against the default branch — a same-SHA branch had zero diff, every file matched paths-ignore, and the workflow was skipped, so a developer creating a personal branch off main to deploy main's exact state to their dev VM (which pins to `:dev-<user>-latest`) had to either commit something or trigger the workflow manually. The `build-and-push` job's `if` was also tightened to `main || workflow_dispatch` only, which prevented branch-push images regardless. Both fixed: added `create:` trigger (filtered to branch refs at the job level so tag creates don't double-build with `keboola-deploy.yml`), and broadened `build-and-push.if` to also publish on non-main branch pushes / branch creates.
- Web header admin nav (All tokens, Marketplaces, Admin → Users / Groups / Resource access / Server config) is now visible to admin users again. Pre-fix, `_app_header.html` gated the admin block on `session.user.role == 'admin'`, but the v13 RBAC migration nulled `users.role` and moved admin authority onto `user_group_members` (Admin system group) — so the gate evaluated to false for everyone, including actual admins. `get_current_user` now injects `user["is_admin"]` (computed via `app.auth.access.is_user_admin`, the same call all server-side admin gates use), and the header reads `session.user.is_admin`. The role badge in the user-menu dropdown now reads "Admin" or hides — `users.role` is no longer surfaced in the UI.
- `admin_tables` register modal payload now matches the `RegisterTableRequest` API contract — drops the phantom `id` and `version` fields the modal used to send (the API silently dropped them), and renames `dataset` → `bucket` so the source-bucket actually persists. Pre-fix the operator's bucket / dataset edit looked saved but never made it past the wire. Edit + delete handlers in the same template were dropping the same fields and are also corrected.
- Discovery JS in `admin_tables` now handles the actual `{tables: [...]}` flat shape returned by `GET /api/admin/discover-tables`. Pre-fix the JS expected `{buckets: [...]}` (a shape the API never emitted) and silently rendered an empty discovery panel after the first call.
- **#108 review fixes for BigQuery register-table.** (a) The post-register materialize worker (BackgroundTask + 5s-timeout daemon thread) no longer captures the request-scoped DuckDB connection — it opens a fresh `get_system_db()` handle per run, so the request's `finally: conn.close()` no longer races the worker. (b) `connectors/bigquery/extractor.init_extract` is now serialized by a module-level `_INIT_EXTRACT_LOCK` so the timeout-fallback BackgroundTask cannot collide with the still-running daemon thread on the `extract.duckdb` swap. (c) `PUT /api/admin/registry/{id}` now runs the same BQ-shape validation as register when the merged record is a BigQuery row (or the patch flips it to BigQuery), returning 400/422 instead of silently persisting an unsafe `bucket` / `source_table` / project_id and breaking at the next rebuild. (d) `POST /api/admin/register-table` no longer carries a misleading `status_code=201` on the route decorator — the Keboola branch explicitly returns 201, the BigQuery branch returns 200 (sync) or 202 (timeout fallback), and OpenAPI now documents all three.
- **#108 round-4 review fix for BigQuery register-table.** `_validate_bigquery_register_payload` now applies the same raw-value rule to `bucket` and `source_table` as round-3 added for `name`. Pre-fix the helper validated `bucket.strip()` / `source_table.strip()` but `register_table` persisted the un-stripped value, so a `bucket=" my_dataset"` slipped through validation, got stored verbatim, and 500'd at the next rebuild when the BQ extractor spliced it into `ATTACH … AS bq_<bucket>` and view DDL. The validator now rejects any `bucket` / `source_table` with leading/trailing whitespace and surfaces the offending raw value in the 400 detail. Applies identically to `POST /api/admin/register-table` and `POST /api/admin/register-table/precheck`.
- **#108 round-3 review fixes for BigQuery register-table.** (a) `_validate_bigquery_register_payload` now validates the **raw** view name (the value persisted to `table_registry.name` and read back by the BQ extractor), not a normalized `strip().lower().replace(" ", "_")` form. Pre-fix a name like `"my table"` passed validation (normalized `"my_table"` was safe), got stored verbatim, and then 500'd at the post-insert rebuild — defeating fast-fail-at-register. The validator now rejects any name with leading/trailing whitespace OR that fails the strict `^[a-zA-Z_][a-zA-Z0-9_]{0,63}$` check, and surfaces the offending raw value verbatim in the 400 response so the operator can retype with a corrected name. Server does NOT silently rewrite the input. Applies identically to `POST /api/admin/register-table` and `POST /api/admin/register-table/precheck`. (b) `_run_bigquery_materialize_with_timeout` now distinguishes worker-raised-within-budget (→ `{"status": "errors"}` → HTTP 500 with the exception in the body) from worker-still-running-at-timeout (→ `{"status": "timeout"}` → HTTP 202 + BackgroundTask retry). Pre-fix both outcomes mapped to "timeout" / 202, hiding the real failure for the budget window before the BG retry surfaced the same exception in the logs. (c) `register_table_precheck` is now a plain `def` (was `async def`) — the BQ branch makes synchronous `bigquery.Client(...)` / `client.get_table(...)` calls that would otherwise block the asyncio event loop on an async handler. Mirrors the same conversion already done for `register_table`.
- **#108 round-2 review fixes for BigQuery register-table.** (a) `POST /api/admin/register-table` is now a plain `def` (was `async def`) — the synchronous-materialize path waits on `threading.Event.wait()`, which blocks the asyncio event loop on an async handler and stalls every other request for up to the 5s budget. FastAPI runs sync handlers in a threadpool so the wait is harmless there. (b) `connectors/bigquery/extractor.rebuild_from_registry` now resolves `data_source.bigquery.project` via `app.instance_config.get_value` (deep-merge of static + writable overlay) instead of `config.loader.load_instance_config` (static only). Operators who set the project through `POST /api/admin/configure` got a silent rebuild failure pre-fix — validation passed (validation already used the overlay-aware read) but the rebuild reported "project missing" and the master view never appeared. (c) `register-table` now propagates `rebuild_from_registry` errors as **HTTP 500 with `{"status": "rebuild_failed", "errors": [...]}`** when the synchronous rebuild ran but reported an error (auth failure, missing project, unsafe identifier slipping the validator). Pre-fix those errors were silently logged and the API returned 200 ok. The BackgroundTask path now logs rebuild errors at ERROR level (was WARNING). (d) The admin tables UI's BigQuery register modal now splits precheck and register into two operator-driven clicks — Step 1 fires precheck and surfaces row count / size / column count in the modal AND swaps the primary button to "Register"; Step 2 fires the actual register call only when the operator clicks. Pre-fix the precheck and register fired in a single chained promise, so the operator never got to review the summary before the row was committed. (e) The Keboola register-modal payload now derives `source_table` from the discovered table's storage identifier (`t.id` minus the bucket prefix, e.g. `company` for `in.c-sfdc.company`) via a new hidden `regSourceTable` field. Pre-fix the JS sent `regTableName` (the human-friendly display name) as `source_table`; manual-entry callers fall back to the display name. (f) `da admin discover-and-register` accepts HTTP 200 / 201 / 202 as success (was 201 only); pre-fix every successful BigQuery row counted as an error because BQ register returns 200 (sync OK) or 202 (background) but never 201.

### Internal

- `_sanitize_for_audit` now masks against an explicit `_SECRET_FIELDS` allowlist instead of substring-scanning + maintaining a `primary_key` whitelist exception. New tests assert `not_actually_a_token` / `primary_key_hash` / `passwordless` flow through cleartext while known-secret fields (`keboola_token`, `client_secret`, `smtp_password`, `bot_token`) get masked. Operationally identical for the current registry payloads (no secret-bearing fields), but removes a class of false-positive / false-negative as the request body grows.
- `release.yml` adds an `e2e-bind-mount` job that boots the freshly built image against a host-bind-mounted `/data` directory (instead of the named volume the existing `smoke-test` job uses). Docker initializes a fresh named volume by copying from the image's `/data` — which the Dockerfile chowns to `agnes:agnes` before flipping USER — so the named-volume path always works. The bind-mount path mirrors what GCE VMs run via `docker-compose.host-mount.yml`, and includes a negative assertion (write must fail on root-owned `/data` before the operator chown) plus a positive assertion (smoke passes after the chown). Locks in the contract that broke a recent release: removing `chown 999:999` from `startup-script.sh.tpl` or changing the Dockerfile uid pin breaks CI.
- Extracted `bigquery.extractor.rebuild_from_registry()` from the `__main__` block of `connectors/bigquery/extractor.py` so the API can call it post-register without `runpy`-importing the module. The standalone CLI entrypoint (`python -m connectors.bigquery.extractor`) keeps working.

## [0.16.0] — 2026-04-29

Minor release. Comprehensive deploy safety audit — CI/CD pipeline hardening, 50+ new tests covering previously untested failure modes, DB schema health check, config versioning, and BigQuery ATTACH error resilience. Built on top of v0.15.0 / `2e1dfb7`.

PR: [#120](https://github.com/keboola/agnes-the-ai-analyst/pull/120) (ci/deploy-safety-audit).

### Added

- **ruff lint + mypy type check** in `release.yml` and `keboola-deploy.yml` CI workflows (both `continue-on-error: true` initially — 257 pre-existing ruff errors, mypy has pre-existing issues; neither blocks CI yet).
- **Automatic rollback** on smoke test failure in `release.yml` — tags the broken image as `:deprecated-<short-sha>`, reverts `:stable` to the previous good tag, opens a GitHub issue for investigation.
- **Smoke test in `keboola-deploy.yml`** — was completely missing; now runs the same `smoke-test.sh` as `release.yml`.
- **Expanded smoke-test.sh** — added `/api/catalog`, `/api/admin/tables`, `/marketplace.zip`, `/api/metrics` endpoint checks beyond the original `/api/health`.
- **Post-deploy smoke test** (`scripts/ops/post-deploy-smoke-test.sh`) — validates health, DB schema version, query endpoint, catalog, and marketplace on a prod VM after deploy.
- **DB schema version check** in `/api/health` — returns `db_schema: "ok" | "mismatch" | "unreachable"`; overall status becomes `"unhealthy"` on schema mismatch. Lets load balancers and monitoring detect half-migrated instances.
- **Config versioning** — `config_version: 1` in `instance.yaml`, validated at startup by `_validate_config_version()` in `config/loader.py`. Prevents silent misconfiguration when the config schema evolves.
- **`.github/settings.yml`** — required status checks on `main` branch.
- **`.github/dependabot.yml`** — weekly pip + github-actions dependency updates.
- **`.github/CODEOWNERS`** — default `@keboola/agnes-team`, special owners for `/infra/`, `/app/auth/`, `src/db.py`.
- **`.pre-commit-config.yaml`** — detect-private-key, check-yaml/json/merge-conflict, ruff, mypy.
- **`[tool.ruff]`** config in `pyproject.toml` — `line-length = 120`, `target-version = "py313"`.

### Test Coverage (~50 new tests)

- **v13→v14 migration** (`test_db.py`): orphan cleanup, FK constraints, rollback on failure.
- **Email magic link TTL** (`test_auth_providers.py`): expired token, token reuse, wrong token.
- **PAT** (`test_pat.py`): malformed JWT, empty bearer, `last_used_ip` tracking.
- **Marketplace ZIP** (`test_marketplace_server_zip.py`): ETag/304, PAT auth, content-addressed caching, `invalidate_etag_cache()` on mutation.
- **Marketplace Git** (`test_marketplace_server_git.py`): smart HTTP, Basic auth with PAT, RBAC filtering.
- **Jira webhooks** (`test_jira_webhooks.py`): HMAC validation, missing signature, malformed JSON (10 tests).
- **Hybrid Query BQ** (`test_remote_query.py`): `register_bq`, JOIN local+BQ, error handling (12 tests).
- **Keboola extractor** (`test_keboola_extractor.py`): crash, partial write, timeout, extension fallback (9 tests).
- **BigQuery extractor** (`test_bigquery_extractor.py`): corrupted DB, partial write, atomic swap, ATTACH timeout (6 tests).
- **Orchestrator** (`test_orchestrator.py`): corrupted extract.duckdb, empty `_meta`, mid-write, unsafe identifiers (5 tests).

### Fixed

- **BigQuery extractor ATTACH error handling** — `init_extract()` now catches exceptions on `INSTALL`/`ATTACH` and records them in `stats["errors"]` instead of propagating up. A network timeout or auth failure no longer crashes the extractor; all configured tables are marked as skipped.
- **ETag cache invalidation on disk mutation** — `invalidate_etag_cache()` is the documented way to force re-hash after marketplace sync. Tests now call it after mutating on-disk content.

### Internal

- `fetch-depth: 0` + `fetch-tags: true` in `release.yml` for rollback tag resolution.
- Docs updated: `ARCHITECTURE.md`, `docs/DATA_SOURCES.md`, `docs/QUICKSTART.md`, `docs/RBAC.md`, `docs/auth-groups.md`.

## [0.15.0] — 2026-04-29

Minor release. Adds corporate memory v1+v1.5 and /me/debug self-only auth diagnostic. See [GitHub release](https://github.com/keboola/agnes-the-ai-analyst/releases/tag/v0.15.0) for full notes.

## [0.14.0] — 2026-04-28

Minor release. Replaces BigQuery wrap-view pattern with Claude-driven fetch primitives. See [GitHub release](https://github.com/keboola/agnes-the-ai-analyst/releases/tag/v0.14.0) for full notes.

## [0.13.0] — 2026-04-28

Minor release. Admin server-config editor + Windows PowerShell wrapper. See [GitHub release](https://github.com/keboola/agnes-the-ai-analyst/releases/tag/v0.13.0) for full notes.

## [0.12.1] — 2026-04-28

Patch release. Hotfixes the pre-migration snapshot-integrity bug shipped in [v0.12.0](https://github.com/keboola/agnes-the-ai-analyst/releases/tag/v0.12.0) and bundles the security/ops hardening from issue groups #82 (auth hardening), #85 (API validation), #87 (deploy posture), plus #46 (SSRF) and #90 (memory stats blocking).

### Added

- Path-traversal validation on `/api/data/{table_id}/download` — `table_id` is
  now checked against `_SAFE_QUOTED_IDENTIFIER` regex (allows dots and hyphens
  for Keboola-style IDs like `in.c-crm.orders`) before any filesystem or DB
  operation; unsafe values return 404 (no info leakage). See issue #85/C2.
- SSRF protection on `POST /api/admin/configure` — `keboola_url` is validated
  against private/reserved networks (127.0.0.0/8, 10.0.0.0/8, 172.16.0.0/12,
  192.168.0.0/16, localhost, IPv6 loopback/link-local/unique-local). Uses
  DNS resolution + `ipaddress` module for robust IPv6 handling (catches
  abbreviated forms like `fe80::1`, `fc00::1`). See issue #46.
- Caddyfile security headers: `X-Frame-Options DENY`,
  `X-Content-Type-Options nosniff`,
  `Referrer-Policy strict-origin-when-cross-origin`, `-Server` (strip).
  See issue #87/M22.
- Container runs as non-root user `agnes` — `USER` directive added to
  Dockerfile with `useradd` + `chown`. See issue #87/C13.
- Docker resource limits: `mem_limit: 4g`, `mem_reservation: 1g`,
  `cpus: 2.0` on `app`; `mem_limit: 2g`, `cpus: 1.0` on `scheduler`.
  See issue #87/M21.
- Startup warning when no user has `password_hash` — alerts operators that
  `/auth/bootstrap` is reachable. See issue #82/C8.
- Audit logging for failed web form login attempts (`/auth/password/login/web`)
  — mirrors the existing `/auth/token` audit trail. See issue #82/M9.
- `/api/health/detailed` endpoint (authenticated) — returns full diagnostics
  (version, schema, sync state, user count). Minimal `/api/health` (unauth)
  returns only `{"status": "ok"}` for load balancers. See issue #87/M17.
- Health endpoint monitoring guide in `docs/DEPLOYMENT.md` — documents both
  endpoints and how to wire external monitoring tools (Datadog, Prometheus,
  UptimeRobot) to `/api/health/detailed` with a PAT.

### Changed

- **BREAKING** `docker-compose.override.yml` renamed to `docker-compose.dev.yml`.
  Docker Compose auto-merges `docker-compose.override.yml` on every host with
  the repo, silently enabling dev mode (source mount + `--reload`) on
  production. The new name requires explicit `-f docker-compose.dev.yml`,
  eliminating the foot-gun. Update any scripts or workflows that relied on
  auto-merge. `scripts/run-local-dev.sh` and `Makefile` updated accordingly.
  See issue #87/M23.
- **BREAKING** `/api/health` now returns a minimal `{"status": "ok"}` payload
  (unauthenticated, for load balancers). Full diagnostics moved to
  `/api/health/detailed` (requires authentication). Scripts that parsed
  `/api/health` for version, sync state, or user count must switch to
  `/api/health/detailed` with an `Authorization` header. CLI commands
  (`da setup test-connection`, `da setup verify`, `da diagnose`, `da status`)
  updated to call `/api/health/detailed` for service-level checks, with
  graceful fallback to the minimal endpoint when auth is not configured.
  See issue #87/M17.
- `release.yml` CI workflow: `build-and-push` job now only runs on `main`
  pushes or manual `workflow_dispatch` triggers. Non-main branch pushes run
  tests only. Added `paths-ignore` for `docs/**`, `*.md`, `LICENSE`.
  See issue #87/M26.

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
- `reset_token` no longer leaks in the JSON response body of
  `POST /api/users/{id}/reset-password`. The `reset_url` still contains the
  token (as intended), but the raw secret is no longer exposed to DevTools,
  proxy logs, or CLI stdout. CLI `admin reset-password` now prints the URL
  instead of the bare token. See issue #82/C5.
- `/api/memory/stats` no longer blocks the async event loop — replaced
  `repo.list_items(limit=10000)` + Python loop with a single SQL
  `GROUP BY` aggregation. See issue #90.
- Magic-link token consumption is now atomic — compare-and-swap pattern
  with a unique `CONSUMED:` marker prevents two concurrent verifies from
  both succeeding. DuckDB concurrent-write conflicts are caught and
  converted to 401. See issue #82/M10.
- Password reset confirm (`POST /auth/password/reset/confirm`) now uses
  the same compare-and-swap pattern as the magic-link flow — closes the
  remaining asymmetry on `users.reset_token` consumption. Lower severity
  than the magic-link race because the reset flow ends with a new
  password (an attacker would need the reset token *and* to race the
  legitimate user) but the consistency closes a polish gap. New
  regression `test_concurrent_reset_only_one_wins` in
  `tests/test_password_flows.py::TestResetConfirm`.
- Upload endpoints (`/sessions`, `/artifacts`) now stream to a temp file with
  cumulative size check instead of buffering the entire body in memory before
  the size cap — prevents OOM from oversized uploads. Temp file handle is
  properly closed before `shutil.move` to avoid FD leaks. See issue #85/M4.
- `/api/upload/local-md` uses a SHA-256 hashed filename instead of raw
  `user_email` — stable per user, no charset surprises from email addresses.
  See issue #85/M4.
- `/auth/bootstrap` 403 message no longer leaks user count. See issue #82/n1.

### Internal

- New regression `test_split_brain_future_version_with_missing_tables_self_heals`
  in `tests/test_db.py::TestMigrationSafety` — synthesizes a v99 DB whose only
  table is `schema_version`, runs `_ensure_schema`, asserts that the v13-era
  core tables (`users`, `user_groups`, `user_group_members`, `resource_grants`)
  get materialized *and* that `schema_version` stays at 99 (self-heal without
  falsely advertising a downgrade).
- New regression `test_pre_migration_snapshot_excludes_post_self_heal_tables`
  pins the snapshot-integrity contract: a v2→vN migration's snapshot must not
  contain any post-v2 table from the modern binary. Sanity-checked against the
  pre-fix unconditional hoist — fails with 6 leaked tables.
- `test_future_version_is_noop` docstring updated to reflect that the
  self-heal pass *does* run on a future-version DB, just doesn't touch the
  version row. The test still passes unchanged — its only assertion was the
  version-row contract, which holds.
- `test_no_override_file` regression test asserts `docker-compose.override.yml`
  does not exist post-rename. See issue #87/M23.

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

- `/api/v2/{catalog,schema,sample,scan,scan/estimate}` — discovery + scoped fetch primitives for remote-mode tables. See `docs/superpowers/specs/2026-04-27-claude-fetch-primitives-design.md`.
- `da catalog`, `da schema`, `da describe`, `da fetch`, `da snapshot {list,refresh,drop,prune}`, `da disk-info` — CLI primitives backed by the v2 API.
- `cli/skills/agnes-data-querying.md` — Claude rails skill loaded for Agnes-flavored projects; covers discovery-first protocol, `da fetch` workflow, BigQuery SQL flavor cheat-sheet, and snapshot hygiene.
- `cli/skills/agnes-table-registration.md` — admin-side companion skill: when to register single vs. bulk-discover, source-side verification before registration, idempotence rules, update/delete via REST (no CLI today), and confirmation flow.
- `instance.yaml: api.scan.*` knobs — `max_limit`, `max_result_bytes`, `max_concurrent_per_user`, `max_daily_bytes_per_user`, `bq_cost_per_tb_usd`, `request_timeout_seconds`. All optional; defaults applied if absent.
- `instance.yaml: api.catalog_cache_ttl_seconds`, `api.schema_cache_ttl_seconds`, `api.sample_cache_ttl_seconds` — TTL knobs for server-side discovery caches.
- `instance.yaml: data_source.bigquery.legacy_wrap_views` — opt-in toggle to restore the pre-v2 behavior of exposing BigQuery VIEW/MATERIALIZED_VIEW tables as DuckDB master views in `analytics.duckdb`. Default `false`. Set `true` for one release cycle when migrating existing scripts (see **BREAKING** note below).
- `instance.yaml: data_source.bigquery.billing_project` — optional GCP project to bill BQ jobs to / submit jobs from. Defaults to `data_source.bigquery.project` for backwards compatibility. Set when the SA has `bigquery.data.*` on the data project but lacks `serviceusage.services.use` there (cross-project read pattern); otherwise `/api/v2/scan/estimate` and BQ-mode `/api/v2/scan` fail with 403.
- **BigQuery extractor detects table type** (BASE TABLE vs. VIEW / MATERIALIZED_VIEW) via `INFORMATION_SCHEMA.TABLES` using DuckDB's `bigquery_query()` table function. Emits the appropriate DuckDB view:
  - BASE TABLE → direct `bq."dataset"."table"` reference (queries hit BigQuery Storage Read API).
  - VIEW / MATERIALIZED_VIEW → `bigquery_query('project', 'SELECT * FROM \`dataset.table\`')` wrapper (queries hit BigQuery Jobs API, required for views).
- **GCE metadata-server authentication for BigQuery.** New `connectors/bigquery/auth.py` module (`get_metadata_token()` function) fetches OAuth access tokens from the GCE metadata server on GCE instances. No service-account key file required. Both the extractor (at sync time) and the orchestrator / read-side (at ATTACH time) fetch fresh tokens on every rebuild / readonly-conn open. Raises `BQMetadataAuthError` on failure (network or malformed metadata-server response).
- **SQL identifier-validation helper** in `src/sql_safe.py`. New functions `is_safe_identifier()` and `validate_identifier()` enforce safe character sets before f-stringing identifiers into SQL. BigQuery extractor and orchestrator `_attach_remote_extensions` both validate `dataset`, `source_table`, and view names before use, closing a SQL-injection surface if admin config is untrusted.
- **`/api/sync/manifest` response now includes `query_mode` and `source_type` per table**, joined from `table_registry`. Clients can branch on table semantics (remote vs. local, source type) without a second API call.
- **`da sync --json` output** now includes a `skipped_remote` list with IDs of `query_mode='remote'` tables that were skipped during sync (they're not downloaded locally; only queried via `/api/query`).
- **Schema v10** introduces `view_ownership` to detect cross-connector view-name collisions in the master analytics DB (issue #81 Group C). When two connectors register the same `_meta.table_name`, the orchestrator now refuses to silently overwrite the prior owner's view — it logs a `view_ownership collision` ERROR identifying both sources and the colliding name, and the second source's view is NOT created. Previously this was last-write-wins, which depended on directory iteration order and could change deployment-to-deployment. Operators resolve a collision by renaming `name` in `table_registry` on one side (registry-side aliasing — `source_table` stays unchanged, only the view name changes). The orchestrator pre-scans every connector's `_meta` at the start of each rebuild and releases stale ownerships immediately (when ALL pre-scans succeed; if any fail, reconcile is skipped to avoid silently stealing a transient-IO source's name), so a renamed table frees its name in the SAME rebuild that introduces the rename — no two-step waits needed. New module `src/repositories/view_ownership.py` exposes the repository.

### Changed

- **BREAKING:** BigQuery `VIEW` and `MATERIALIZED_VIEW` tables (i.e. `query_mode='remote'` tables whose underlying BQ object is a view) are no longer wrapped as DuckDB master views in `analytics.duckdb`. `da query --remote "SELECT * FROM <bq_view>"` no longer resolves the view name by default. Use `da fetch <table_id> --where ... --as <snapshot_name>` to materialize a local snapshot, or `da query --remote "SELECT ... FROM bigquery_query('<project>', '<inner BQ SQL>')"` for one-shot execution. To restore the previous behavior for a migration window, set `instance.yaml: data_source.bigquery.legacy_wrap_views: true`. BQ `BASE TABLE` entities are unaffected — their direct-ref master views remain.
- **`da sync` skips `query_mode='remote'` tables.** Previously they produced 404s on download attempts. Now the CLI prints a one-line stderr summary (`Skipping N remote-mode tables: a, b, c (and M more)`) and a separate summary line (`Skipped (remote-mode): N`) in the final output, distinct from existing `Skipped (unchanged): M` counts.

### Fixed

- **`/api/v2/scan` 500 on local-mode tables.** `arrow_table_to_ipc_bytes()` only handled `pa.Table`; DuckDB's local query path returns a `pa.RecordBatchReader`. Helper now accepts both. (Caught during dev-VM E2E.)
- **`/api/v2/schema/{table_id}` 500 on BigQuery tables.** `_fetch_bq_schema()` selected `description` from `INFORMATION_SCHEMA.COLUMNS`, which BigQuery doesn't expose there — column descriptions live in `INFORMATION_SCHEMA.COLUMN_FIELD_PATHS` for nested fields. Removed the column from the SELECT; descriptions default to empty string until a real source is wired. (Caught during dev-VM E2E.)
- **BigQuery views failed at first query when FastAPI / CLI reopened `analytics.duckdb`.** `SyncOrchestrator._attach_remote_extensions` fetches a fresh GCE-metadata access token and creates a `bigquery` DuckDB SECRET before ATTACH, but secrets are session-scoped and don't persist with the on-disk database. The mirror code in `src.db._reattach_remote_extensions` (called from `get_analytics_db_readonly()`) still ATTACHed BigQuery without auth, so the next query against `bq."dataset"."table"` failed. Fixed by adding the same three-branch structure to `src.db`: BigQuery → fetch metadata token → `CREATE OR REPLACE SECRET bq_secret_<alias> (TYPE bigquery, ACCESS_TOKEN '<token>')` → ATTACH; otherwise fall back to env-var-token / no-auth paths. Metadata-server failures log at ERROR and skip the source so other connectors still resolve.
- **`src/orchestrator.py::_attach_remote_extensions` was ineffective for BigQuery.** It filtered `_remote_attach` lookups by `table_schema=<source_name>`, but DuckDB lists an attached database with `table_catalog=<source_name>` (not `table_schema`), so the loop never executed and `_remote_attach` rows were silently ignored. Switched the filter to `table_catalog`, matching the corresponding query already in `src.db`.
- **BigQuery extractor `python -m connectors.bigquery.extractor` standalone CLI** now reads project ID from `data_source.bigquery.project` matching `instance.yaml.example`. Previously it looked for an undocumented top-level `bigquery.project_id` key and silently produced an empty string on miss, causing cryptic BigQuery API errors downstream. Now exits with code 2 + a clear `logger.error` when the key is missing.

### Internal

- Test pattern: BigQuery extractor is exercised with a dual-path strategy (BASE TABLE + VIEW detection) via `_CapturingProxy` SQL-capture wrappers. DuckDB's C-implemented `execute` attribute is read-only and can't be monkey-patched directly; the proxy wraps the connection and captures outgoing SQL before forwarding to the real DuckDB conn.
- Implementation plan: `docs/superpowers/plans/2026-04-27-bq-pipeline-views-and-metadata-auth.md` — subagent-driven development for Tasks 1-7 of this PR.

### Changed (issue #81 / #44 / #88 — security & OSS neutralization)

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

---

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

---

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

[0.16.0]: https://github.com/keboola/agnes-the-ai-analyst/releases/tag/v0.16.0
[0.15.0]: https://github.com/keboola/agnes-the-ai-analyst/releases/tag/v0.15.0
[0.14.0]: https://github.com/keboola/agnes-the-ai-analyst/releases/tag/v0.14.0
[0.13.0]: https://github.com/keboola/agnes-the-ai-analyst/releases/tag/v0.13.0
[0.12.1]: https://github.com/keboola/agnes-the-ai-analyst/releases/tag/v0.12.1
[0.12.0]: https://github.com/keboola/agnes-the-ai-analyst/releases/tag/v0.12.0
[0.11.5]: https://github.com/keboola/agnes-the-ai-analyst/releases/tag/v0.11.5
[0.11.4]: https://github.com/keboola/agnes-the-ai-analyst/releases/tag/v0.11.4
[0.11.3]: https://github.com/keboola/agnes-the-ai-analyst/releases/tag/v0.11.3
[0.11.2]: https://github.com/keboola/agnes-the-ai-analyst/releases/tag/v0.11.2
[0.11.1]: https://github.com/keboola/agnes-the-ai-analyst/releases/tag/v0.11.1
[0.11.0]: https://github.com/keboola/agnes-the-ai-analyst/releases/tag/v0.11.0
