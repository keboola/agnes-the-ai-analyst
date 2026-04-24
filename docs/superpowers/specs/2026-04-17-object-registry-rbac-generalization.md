# Object Registry — RBAC Generalization

**Date:** 2026-04-17
**Status:** Draft
**Author:** design discussion between @davidrybar-grpn and Claude

## 1. Summary

Generalize Agnes's per-dataset RBAC so the same permission machinery can gate
arbitrary resource types — starting with Claude Code **plugins**, but built to
accept any future resource (metrics, dashboards, scripts, saved queries) without
a new permission system each time.

**Recommendation:** unify the **permissions + access-check layer**, keep
resource catalogs (`table_registry`, future `plugin_registry`, …) typed and
separate. Do not collapse everything into a single wide `object_registry`.

## 2. Context — Why This Came Up

Two motivating scenarios:

1. **Data access control (shipped).** Analysts authenticate via Google OAuth,
   land in DuckDB `users` with a role, and get per-dataset grants through
   `dataset_permissions`. Enforced at FastAPI boundary via `src/rbac.py`.
2. **Plugin marketplace proxy (proposed).** Agnes becomes an authenticated git
   proxy between a private GitHub plugin repo and analysts' Claude Code
   installations. Users don't have direct GitHub access; Agnes decides which
   plugins each user can see and install.

Both problems have the same shape:

> "User X may or may not access resource Y, where Y is one of many concrete
> resource types. Enforce at the API boundary, log to `audit_log`, expose via
> admin UI."

Today's code only solves this for tables. A naïve copy for plugins would create
parallel `plugin_registry` + `plugin_permissions` + `can_access_plugin` +
`/api/admin/plugin-permissions` — a second, drifting RBAC implementation.

## 3. The Design Question

Two plausible generalizations were discussed:

### Option A — Single-Table Inheritance (rejected)

```sql
CREATE TABLE object_registry (
    id          VARCHAR PRIMARY KEY,
    type        VARCHAR NOT NULL,        -- 'table' | 'plugin' | ...
    name        VARCHAR,
    -- table-only fields
    bucket      VARCHAR,
    source_table VARCHAR,
    query_mode  VARCHAR,
    sync_schedule VARCHAR,
    is_public   BOOLEAN,
    profile     JSON,
    -- plugin-only fields
    git_url     VARCHAR,
    version     VARCHAR,
    manifest_sha VARCHAR,
    category    VARCHAR,
    dependencies JSON,
    -- future types keep adding columns...
);
```

One registry, one permissions table, one access function. Maximum DRY.

**Why we reject this:**

- `table_registry` and `plugin_registry` share almost no columns. Any
  single-table merge produces a wide sparse row where ~60% of columns are NULL
  for any given type.
- Validation erodes: "the column doesn't exist for this type" becomes "the
  column exists but is only valid for some types," which the database can't
  enforce.
- Every new resource type adds more nullable columns and more type-guarded
  queries (`WHERE type='table'` sprinkled everywhere).
- Schema migrations for any one type touch the shared table — higher blast
  radius and more migration complexity.

This is the classic **single-table inheritance anti-pattern** when the subtypes
don't share most fields. Rails, Django, and most ORMs distinguish it from
polymorphic associations for exactly this reason.

### Option B — Polymorphic Join, Typed Entities (recommended)

Keep resource catalogs typed and separate (each has only the columns it needs).
Unify only the **permissions table** and the **access-check function**, because
that's the layer where the duplication actually hurts.

```
┌────────────────────┐      ┌─────────────────────┐     ┌─────────────────────┐
│   table_registry   │      │   plugin_registry   │     │   metric_definitions│
│  (tables only)     │      │  (plugins only)     │     │  (metrics only)     │
└────────┬───────────┘      └──────────┬──────────┘     └──────────┬──────────┘
         │                             │                           │
         └──────────────┬──────────────┴───────────────────────────┘
                        ▼
              ┌─────────────────────────┐
              │   object_permissions    │  ← single polymorphic permissions table
              │  (user_id, type, id)    │
              └────────────┬────────────┘
                           ▼
              ┌─────────────────────────┐
              │   can_access(type, id)  │  ← single RBAC function, type-dispatched
              └─────────────────────────┘
```

This gives us the generalization at the layer where it pays off (authorization
logic, admin API, audit logging) without paying the sparse-table cost.

## 4. Proposed Schema

### 4.1 `object_permissions` — new (rename of `dataset_permissions`)

```sql
CREATE TABLE object_permissions (
    user_id     VARCHAR NOT NULL,
    object_type VARCHAR NOT NULL,      -- 'table' | 'plugin' | ...
    object_id   VARCHAR NOT NULL,      -- concrete id or wildcard pattern
    access      VARCHAR DEFAULT 'read',
    PRIMARY KEY (user_id, object_type, object_id)
);
```

Observation: the current `dataset_permissions.dataset` column is *already*
polymorphic in practice — it accepts table IDs, bucket wildcards
(`in.c-finance.*`), and "dataset group" labels. It just isn't typed, which
means a plugin named `revenue` would silently collide with a table named
`revenue`. Adding `object_type` closes that gap.

### 4.2 Resource catalogs — unchanged in shape

- `table_registry` stays as-is.
- `plugin_registry` is added with only the columns a plugin needs:
  `id, name, git_url, version, category, manifest_sha, is_public, mirrored_at`.
- Future types (`metric_definitions` is already there, for example) plug into
  the same permissions layer without touching any other catalog.

## 5. Proposed Code Shape

### 5.1 Generic access check (`src/rbac.py`)

```python
def can_access(user: dict, object_type: str, object_id: str, conn=None) -> bool:
    if user.get("role") == "admin":
        return True

    # type-dispatched "is_public" check — each type decides what public means
    if _IS_PUBLIC[object_type](object_id, conn):
        return True

    perm = ObjectPermissionRepository(conn)
    if perm.has_access(user["id"], object_type, object_id):
        return True

    # type-dispatched wildcard parent (bucket for tables, category for plugins, ...)
    parent = _WILDCARD_PARENT[object_type](object_id, conn)
    if parent and perm.has_access(user["id"], object_type, parent):
        return True

    return False
```

Type-specific behaviour collapses into two dispatch dicts:

```python
_IS_PUBLIC = {
    "table":  _table_is_public,     # reads table_registry.is_public
    "plugin": _plugin_is_public,    # reads plugin_registry.is_public
}

_WILDCARD_PARENT = {
    "table":  lambda tid, c: _get_bucket(tid, c) and f"{_get_bucket(tid, c)}.*",
    "plugin": lambda pid, c: _get_category(pid, c) and f"{_get_category(pid, c)}.*",
}
```

Adding a new resource type = one registry table + two small functions. No other
code moves.

### 5.2 Generic admin API (`app/api/permissions.py`)

```python
class PermissionRequest(BaseModel):
    user_id:     str
    object_type: str                # NEW — required
    object_id:   str                # renamed from 'dataset'
    access:      str = "read"

@router.post("")
async def grant_permission(
    request: PermissionRequest,
    user: dict = Depends(require_role(Role.ADMIN)),
    conn = Depends(_get_db),
):
    ObjectPermissionRepository(conn).grant(
        request.user_id, request.object_type, request.object_id, request.access
    )
```

One endpoint serves all resource types. Admin UI gets a `type` dropdown; the
rest is identical to today.

### 5.3 Call-site updates

Existing call sites become explicit about the type they're gating:

| Today                                              | After                                                         |
|----------------------------------------------------|---------------------------------------------------------------|
| `can_access_table(user, table_id, conn)`           | `can_access(user, "table", table_id, conn)`                   |
| `has_dataset_access(email, dataset)`               | `can_access_by_email(email, "table", dataset)`                |
| `get_accessible_tables(user, conn)`                | `get_accessible(user, "table", conn)`                         |

Roughly 15 lines across `app/api/data.py`, `app/api/catalog.py`,
`app/api/query.py`, `app/api/sync.py`.

## 6. Migration Plan

DuckDB schema bump v4 → v5, executed in `src/db.py` with the existing
auto-migration machinery.

```sql
-- Rename + retype in place. Every existing grant is a table grant.
ALTER TABLE dataset_permissions RENAME TO object_permissions;
ALTER TABLE object_permissions ADD COLUMN object_type VARCHAR;
UPDATE object_permissions SET object_type = 'table' WHERE object_type IS NULL;
-- Primary key change:
ALTER TABLE object_permissions
    DROP CONSTRAINT dataset_permissions_pkey;   -- or equivalent DuckDB pragma
ALTER TABLE object_permissions
    ADD PRIMARY KEY (user_id, object_type, object_id);
-- Rename column for clarity
ALTER TABLE object_permissions RENAME COLUMN dataset TO object_id;
```

Code changes grouped into one PR:

1. Migration v4 → v5.
2. `DatasetPermissionRepository` → `ObjectPermissionRepository` with
   `object_type` parameter on every method.
3. `src/rbac.py` — introduce generic `can_access(type, id)`, keep
   `can_access_table(...)` as a one-line shim calling
   `can_access(..., "table", ...)` for backward compat during rollout.
4. Update the ~15 call sites in `app/api/*.py`.
5. Update admin API (`app/api/permissions.py`) to accept `object_type`.
6. Update tests (`tests/test_rbac.py`, `tests/test_journey_rbac.py`,
   `tests/test_db.py`).

**Estimated effort:** half a day of focused work plus test updates. The logic
doesn't change — only the shape of function signatures and one column.

## 7. Consequences

### 7.1 Immediate benefits

- Plugin RBAC is a schema addition (`plugin_registry` + two dispatch functions)
  instead of a parallel RBAC stack. Unlocks the marketplace-proxy proposal.
- Namespace collision between resource types becomes structurally impossible
  (plugin `revenue` ≠ table `revenue`).
- Single admin API, single audit-log shape, single permissions list in the
  webapp UI — one mental model for admins instead of one per resource type.

### 7.2 Costs

- One DuckDB migration with a primary-key change. Low risk because
  `dataset_permissions` is small and rarely written to, but still a migration
  — back up `system.duckdb` before deploying.
- Every `can_access` call becomes slightly more verbose (an extra string
  argument). This is a deliberate cost: the type is now explicit at every call
  site, which is easier to audit for correctness than guessing from context.
- Admin UI needs a `type` selector. Trivial in the webapp; worth designing
  before adding the second type so the filter lives from day one.

### 7.3 Non-consequences

- No impact on data-path performance. `object_permissions` has the same row
  count and query shape as `dataset_permissions` today.
- No impact on the FastAPI dependency-injection patterns
  (`require_role`, `get_current_user`, `_get_db`).

## 8. What This Is *Not*

- **Not** a merged entity catalog. Tables and plugins keep their own registries
  with their own columns. We learned that lesson from single-table inheritance.
- **Not** a new authorization framework. This is a rename + a type column plus
  two dispatch dicts. No new concepts, no new dependencies.
- **Not** a blocker for the marketplace proxy. The marketplace proxy can ship
  with `plugin_permissions` as a standalone table and be migrated to
  `object_permissions` later — but if we know we're heading here, paying the
  half-day migration cost upfront avoids a second drifting implementation.

## 9. Open Questions

1. Do we want a top-level "object groups" concept (like "dataset groups" today
   implemented as wildcard strings), or is the bucket/category wildcard pattern
   sufficient?
2. Should `access` values become type-specific (e.g., `read` / `install` for
   plugins, `read` for tables), or stay as a uniform `read` / `none` across
   types? Current recommendation: keep uniform, model per-type semantics as
   presence/absence of grant.
3. Should the `audit_log` schema add an `object_type` column? Yes — for
   symmetry and to enable per-type audit queries. Cheap to add in the same
   migration.

## 10. Decision Record

- **Decision:** adopt Option B (polymorphic permissions, typed registries).
- **Rejected:** Option A (unified `object_registry` via single-table
  inheritance).
- **Rationale:** generalization belongs at the verb layer (permission checks),
  not the noun layer (resource catalogs). The two subtypes of "resource"
  already share almost no attributes, so unifying their storage adds
  nullability and coupling without removing real duplication. The permissions
  table, by contrast, is already polymorphic in practice — typing it makes
  that explicit and safe.
