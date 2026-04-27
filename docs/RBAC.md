# Access control (v12)

Two-layer authorization model:

- **App-level access** = membership in the seeded `Admin` user-group. Admins can do everything; everyone else is gated through resource grants.
- **Resource-level access** = generic `(group, resource_type, resource_id)` grants. A user has access to a specific resource if any of their groups holds a matching grant.

There is no role hierarchy, no session cache, no implies expansion, no module-author registration step. Every protected endpoint resolves authorization with one or two DuckDB queries.

---

## Tables

| Table | Purpose |
|---|---|
| `user_groups` | Named groups. Two rows seeded as `is_system=TRUE`: **Admin** (god mode) and **Everyone** (auto-membership for all users). |
| `user_group_members` | `(user_id, group_id, source)`. `source ∈ {admin, google_sync, system_seed}` so each writer only manipulates its own rows — Google sync's nightly DELETE+INSERT does not clobber admin-added members. |
| `resource_grants` | `(group_id, resource_type, resource_id)`. The grant table the resolver hits when Admin short-circuit doesn't apply. |

`resource_type` is a string from the `app.resource_types.ResourceType` `StrEnum`. `resource_id` is a path string whose format is owned by the registering module — for `marketplace_plugin` it's `<marketplace_slug>/<plugin_name>`.

---

## Authorization API

```python
from app.auth.access import require_admin, require_resource_access
from app.resource_types import ResourceType

# App-level — admin actions, settings, user management.
@router.post("/admin/users")
async def create_user(user = Depends(require_admin)): ...

# Resource-level — entity-scoped reads/writes.
@router.get("/marketplace/{slug}/plugins/{name}")
async def get_plugin(
    slug: str, name: str,
    user = Depends(require_resource_access(
        ResourceType.MARKETPLACE_PLUGIN, "{slug}/{name}",
    )),
): ...
```

The `path_template` argument is a Python format string resolved against the request's `path_params` at gate time — `"{slug}/{name}"` becomes the `resource_id` for the grant lookup.

Admin short-circuits both helpers — admins never need explicit grants.

---

## Adding a new resource type

1. Add an enum member in `app/resource_types.py`:

   ```python
   class ResourceType(StrEnum):
       MARKETPLACE_PLUGIN = "marketplace_plugin"
       DATASET = "dataset"  # new
   ```

2. Add metadata for the admin UI:

   ```python
   RESOURCE_TYPE_META[ResourceType.DATASET] = {
       "display_name": "Dataset",
       "description": "A table available in the analytics catalog.",
       "id_format": "<bucket>.<table_name>",
   }
   ```

3. Wire your endpoints with `require_resource_access(ResourceType.DATASET, "{bucket}.{table}")`.

No DB migration, no startup hook. The admin UI's resource-type dropdown reads `/api/admin/resource-types` which projects this dict.

---

## Group membership sources

Members are added to groups by three sources, distinguished by the `source` column:

- **`google_sync`** — written by the OAuth callback on every login. The previous Google-sync set is wholesale replaced (DELETE + INSERT) so a removed Workspace membership disappears immediately.
- **`admin`** — written by admin actions in the UI (`/admin/access`), CLI (`da admin group add-member …`), or REST (`POST /api/admin/groups/{id}/members`). Survives Google sync. Admin can only delete admin-source rows.
- **`system_seed`** — written at deploy time. Used for the `SEED_ADMIN_EMAIL` → Admin-group binding and the auto-Everyone membership of every new user. Never modified at runtime.

Removing a user from a group via the admin path (UI/CLI/REST) only deletes admin-source rows. To revoke a Google-synced membership, the operator must change the upstream Workspace group instead — Agnes will pick up the change on the user's next login.

---

## Admin workflows

### UI

`/admin/access` is the single admin page. Two tabs:

- **Groups** — list user-groups with member/grant counts. Click a group to manage members. System groups are read-only.
- **Resource grants** — list grants across all groups (filterable by group / resource_type), create new grants via dropdowns wired against `/api/admin/resource-types`.

`/admin/users/{id}` (the existing user detail page) toggles the Admin-group membership when an operator switches a user's "role" between admin and non-admin — there's no four-level hierarchy left, just admin / non-admin.

### CLI

```bash
da admin group list
da admin group create Engineering --description "Eng team"
da admin group delete Engineering
da admin group members Engineering
da admin group add-member Engineering alice@example.com
da admin group remove-member Engineering alice@example.com

da admin grant resource-types
da admin grant create Engineering marketplace_plugin foundry-ai/metrics-plugin
da admin grant list --type marketplace_plugin
da admin grant list --group Engineering
da admin grant delete <grant-id>
```

All subcommands authenticate via PAT and exit non-zero on API errors.

### REST

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/admin/groups` | GET / POST | list / create groups |
| `/api/admin/groups/{id}` | PATCH / DELETE | rename / delete (system groups read-only) |
| `/api/admin/groups/{id}/members` | GET / POST | list / add member |
| `/api/admin/groups/{id}/members/{user_id}` | DELETE | remove (admin-source rows only) |
| `/api/admin/grants` | GET / POST | list (with `?resource_type=` / `?group_id=`) / create |
| `/api/admin/grants/{id}` | DELETE | delete |
| `/api/admin/resource-types` | GET | enumerate the StrEnum |

Every mutation writes an audit log entry (`user_group.created`, `resource_grant.deleted`, …).

---

## Bootstrapping the first admin

`SEED_ADMIN_EMAIL` (env var, set by the infra Terraform module) points at the operator's email. The app startup hook in `app/main.py`:

1. Creates a `users` row for that email if missing (with `password_hash` from `SEED_ADMIN_PASSWORD` if provided).
2. Adds an Admin-group membership with `source='system_seed'`.

The hook is idempotent — re-running deploy does not duplicate or revoke. To add additional initial admins post-deploy, log in as the seed admin and use `/admin/access` or `da admin group add-member Admin <email>`.

---

## Migration from v9–v11 (schema v12 cutover)

The v11→v12 migration is a single-step hard cutover. The Python helper `_v11_to_v12_finalize` runs after the new tables are created and:

1. Seeds Admin/Everyone in `user_groups` (idempotent).
2. Backfills `user_group_members` from `users.groups` JSON with `source='google_sync'`.
3. Promotes every `core.admin` user-role grant to Admin-group membership with `source='system_seed'`.
4. Adds Everyone-group membership for every existing user.
5. Translates `plugin_access` rows to `resource_grants` of type `marketplace_plugin`, resource_id `<marketplace>/<plugin>`.
6. Drops `plugin_access`, `user_role_grants`, `group_mappings`, `internal_roles` (FK-correct order).
7. Drops the `users.groups` JSON column. The legacy `users.role` column is kept NULL'd as an artifact (DuckDB historical FK constraints sometimes block DROP COLUMN; the field carries no semantic meaning post-v12).

No dual-write window. Either the schema is on v11 (old code) or v12 (new code).
