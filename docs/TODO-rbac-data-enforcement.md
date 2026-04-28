# TODO — RBAC data enforcement (drop `dataset_permissions`)

## Goal

Switch table-access authorization from the legacy per-user
`dataset_permissions` table to the unified per-group `resource_grants`
model (via `ResourceType.TABLE`), then drop `dataset_permissions`
entirely.

The first step — surfacing tables on the `/admin/access` page as a new
resource type — has already shipped. Admins can grant/revoke per-group,
but the runtime check still walks the legacy code path. This document
describes the follow-up work that closes the loop.

## Background

Two parallel access models exist today:

- **v13 unified model** — `user_groups` ← `user_group_members` ←
  `resource_grants(group_id, resource_type, resource_id)`. Used by
  `MARKETPLACE_PLUGIN` and (for listing only) `TABLE`.
- **Legacy per-user model** — `dataset_permissions(user_id, dataset)`
  with `bucket.*` wildcards and an `is_public` bypass on
  `table_registry`. Used at runtime by `src/rbac.py:can_access_table`,
  which is the call site referenced by `app/api/catalog.py`,
  `app/api/sync.py`, and any other endpoint that filters tables by
  caller.

The two layers are mutually exclusive — `dataset_permissions` is
per-user, `resource_grants` is per-group. v13 already pivoted marketplace
plugins to groups; tables are the last holdout.

## Steps

1. **Refactor `src/rbac.py:can_access_table`** (line 63). Preserve the
   external signature — callers stay untouched. New check order:
   1. Admin bypass (already in place).
   2. `is_public=True` on `table_registry` row → allow.
   3. Delegate to
      `app.auth.access.can_access(user_id, ResourceType.TABLE.value, table_id, conn)`.

   Remove the `DatasetPermissionRepository.has_access(user_id, table_id)`
   branch and the `bucket.*` wildcard branch. The bucket-level "grant
   all" UX is now expressed via the per-table bulk action in
   `/admin/access`, not via a wildcard token.

2. **Audit callers of `can_access_table`.** Run
   `grep -rn 'can_access_table\|get_accessible_tables' --include='*.py'`
   and confirm each site still works with the new semantics. Known
   sites: `app/api/catalog.py:24`, `app/api/catalog.py:90`,
   `app/api/sync.py`. Either keep delegating through `can_access_table`
   (cheapest), or migrate the endpoint to
   `Depends(require_resource_access(ResourceType.TABLE, "{table_name}"))`
   for endpoints that have a clean `{table_name}` path param.

3. **Schema v14 migration in `src/db.py`.**
   - Decide on backfill strategy for existing `dataset_permissions`
     rows. Two options:
     - **(a) Backfill** — for each `(user_id, dataset)` row, ensure a
       personal group `user-{email}` exists, add the user as a member
       (source `system_seed`), and insert
       `resource_grants(group_id, "table", dataset)`. Wildcard rows
       (`bucket.*`) need expansion to all matching `table_registry.id`
       values at migration time.
     - **(b) Drop without backfill** — log a CHANGELOG warning,
       require admins to re-grant via `/admin/access` post-deploy.
   - **Recommended: (b)**, contingent on an ops check that
     `dataset_permissions` is not heavily populated in any active
     deployment. (b) is much simpler and avoids creating "groups of one"
     that clutter the access UI.
   - Drop the `dataset_permissions` table.
   - Decide whether `access_requests` (`src/db.py:183`) goes too —
     it implements the "I'd like access to X" flow that pairs with
     `dataset_permissions`. If retained, it should write to
     `resource_grants` on approval; if not used in practice, drop it
     in the same migration.

4. **Remove `DatasetPermissionRepository`** from
   `src/repositories/sync_settings.py:47`. Grep for any remaining
   importers and clean them up.

5. **Update `tests/test_access_control.py`.** The current suite seeds
   `dataset_permissions` directly. Rewrite the relevant cases to seed
   `resource_grants` via the access API (or directly via
   `ResourceGrantsRepository`). Most assertions should carry over
   verbatim because the public surface (HTTP status codes, manifest
   content) does not change.

6. **CHANGELOG entry under `## [Unreleased]`:**
   ```
   ### Changed
   - **BREAKING** Table access moved from per-user `dataset_permissions`
     to per-group `resource_grants` (via `ResourceType.TABLE`). Existing
     `dataset_permissions` entries are dropped on upgrade — admins must
     re-grant via /admin/access.
   ```

7. **Update `docs/RBAC.md`.** It already calls out v13's removal of
   `plugin_access`; add the analogous note for `dataset_permissions`
   and document the `is_public` bypass alongside the admin bypass.

8. **Delete the dummy seed script.** Once enforcement is wired up and
   verified end-to-end against a real data source, remove
   `scripts/seed_dummy_tables.py` and the corresponding `### Internal`
   entry from `CHANGELOG.md`. The script exists only to make the
   `/admin/access` Tables section testable while no real connector is
   configured; once production-style tables flow through `table_registry`
   via Keboola/BigQuery discovery, the dummy rows just clutter the UI.
   Also drop any leftover dummy rows from deployments that ran the
   script (`DELETE FROM table_registry WHERE source_type = 'dummy'`).

## Open questions

- **Production usage of `dataset_permissions`.** Determines whether
  step 3 needs a backfill. Check with ops or query a representative
  deployment. If essentially empty, prefer (b) and skip the backfill.
- **Fate of `access_requests`.** Does anyone use the request-access
  workflow today? If not, drop it together with `dataset_permissions`.
  If yes, it needs an updated approval path that writes to
  `resource_grants`.
- **`is_public` flag.** Recommended to keep — semantics "default
  visible to everyone, no grant needed" is independent of the
  group-grant model and useful for shared reference tables. Confirm
  before locking in.
- **Bucket-level grants.** v1 deliberately models the bucket as a
  UI-only grouping (mirroring how `marketplace` is not a grantable
  resource — only individual plugins are). If, after living with the
  per-table bulk-action UX, admins still want first-class bucket grants
  that auto-cover newly registered tables, add `ResourceType.BUCKET`
  in a later iteration.
