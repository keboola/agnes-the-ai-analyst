# Access control (v14)

Two-layer authorization model:

- **App-level access** = membership in the seeded `Admin` user-group. Admins can do everything; everyone else is gated through resource grants.
- **Resource-level access** = generic `(group, resource_type, resource_id)` grants. A user has access to a specific resource if any of their groups holds a matching grant.

There is no role hierarchy, no session cache, no implies expansion, no module-author registration step. Every protected endpoint resolves authorization with one or two DuckDB queries.

---

## What this model does *not* govern

Agnes authorizes **its own entities** — registered tables, data packages, marketplace plugins, memory domains, and the other `ResourceType` values. It does not reach inside the systems it connects to.

A frequent and consequential misreading is that Agnes can scope a connected system's contents — "restrict this agent to one Confluence space", "let this group see only the tickets in project X". It cannot. When Agnes talks to an upstream system it does so as whatever principal the connection was configured with (a service account, an API token, a workload identity), and **that principal's permissions in the target system are the real boundary**. Agnes governs who may reach a connection; the target system governs what that connection can see.

The practical consequences:

- **Scope at the source.** If a group must not see a Confluence space or a Jira project, that restriction belongs on the service account in Confluence/Jira — typically as a second, narrower connection. Granting or withholding the Agnes-side resource is all-or-nothing over whatever the credential can reach.
- **A shared service account flattens identity.** Everyone reaching a system through one connection is the same principal upstream, which is the point (people without a seat in the upstream tool can still read through it) and also the cost: the upstream audit log records the service account, not the human. Agnes's own audit trail is where per-user attribution survives.
- **Grants are deterministic and flat.** No nesting, no inheritance, no negative grants. A user's access is the union over their groups — there is no way to express "everything in this package except one table". Split the package instead.
- **Table-level, not row-level, with one exception.** Grants resolve per table. The internal `agnes_sessions` / `agnes_telemetry` / `agnes_audit` tables are the exception: table-level access is universal and a per-row `WHERE` filter is applied at query time (`src/rbac.py`, `connectors/internal/access.py`). Do not model general row-level security on this — it is specific to those tables.

To limit what an *agent* can reach, use agent scopes (an agent's effective authority is owner grants ∩ agent scope, enforced live at every brokered request) — but the same boundary applies underneath: the agent inherits whatever the connection's upstream principal can see.

---

## Tables

| Table | Purpose |
|---|---|
| `user_groups` | Named groups. Two rows seeded as `is_system=TRUE`: **Admin** (god mode) and **Everyone** (auto-membership at creation for every new user by default; Workspace-mirrored instead when `AGNES_GROUP_EVERYONE_EMAIL` is set — see [Group membership sources](#group-membership-sources)). |
| `user_group_members` | `(user_id, group_id, source)`. `source ∈ {admin, google_sync, system_seed}` so each writer only manipulates its own rows — Google sync's nightly DELETE+INSERT does not clobber admin-added members. **v14**: FK constraint on `group_id` referencing `user_groups.id` (cascade delete). |
| `resource_grants` | `(group_id, resource_type, resource_id)`. The grant table the resolver hits when Admin short-circuit doesn't apply. **v14**: FK constraint on `group_id` referencing `user_groups.id` (cascade delete). |

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

### Admin elevation consent gate

The short-circuit is subject to a per-browser consent gate
(`app/auth/elevation.py`): an admin can **pause** their own elevation via
`POST /api/me/elevation` (UI on `/profile`), and while paused the
short-circuit is skipped — `can_access` falls through to the explicit
group-grant path and `require_admin` refuses with the distinct detail
`admin_elevation_paused`. State rides the `agnes_elevation` cookie, read
into a request contextvar by middleware; the cookie can only ever
*reduce* privilege (enforcement remains the server-side Admin-membership
check), so this is a guard against accidental god-mode use plus an audit
hook (`admin_elevation_paused`/`admin_elevation_resumed` audit actions),
not a containment boundary — a paused admin can re-elevate at will.

Scope, precisely: the gate sits in `can_access` and `require_admin`. Surfaces
that consult Admin membership directly rather than going through those — the
table/catalog visibility path is the notable one — are unaffected, so a paused
admin still sees every table. WebSocket routes are unaffected too: the stamping
middleware is `@app.middleware("http")` and never runs for a `ws` scope, so
those connections read the contextvar default (elevated). Both cases fail
toward the historical behavior and neither can grant a non-admin anything.
Widening the gate to the membership checks is a separate change: several of
them decide whether to offer the toggle at all.

The pause belongs to the admin who set it. `can_access` is also asked about
OTHER users (co-drive invites check the invitee), so the request also carries
whose pause it is — a paused admin's own checks fall through to their grants
while a question about a colleague is answered from the colleague's own
permissions.

The instance default is `access.admin_default_elevation: "elevated"`
(historical behavior); set `"paused"` for consent-first deployments.
The default applies to **browser sessions only**: Bearer-authenticated
requests (CLI, PATs, service tokens) carrying no elevation cookie always
run elevated — automation has no cookie jar to re-elevate with, so a
paused default must not 403 every `agnes admin …` call (an explicit
`paused` cookie is still honored even alongside a Bearer header).
Non-HTTP contexts (scheduler internals, background jobs) likewise run
elevated via the contextvar default. Each god-mode grant of a
resource the admin holds no explicit grant for emits a deduplicated
`god_mode_bypass` log line — the observability data for deciding where
explicit grants should replace god-mode reliance.

---

## Adding a new resource type

Everything lives in `app/resource_types.py`. Three edits, one file:

1. Add an enum member to `ResourceType`:

   ```python
   class ResourceType(StrEnum):
       MARKETPLACE_PLUGIN = "marketplace_plugin"
       DATASET = "dataset"  # new
   ```

2. Write a `list_blocks` delegate (no arguments) that reads through the `src.repositories` factory and projects the domain tables into the `(block → items)` shape the admin /access page consumes. Each item must include `resource_id` matching the path string written into `resource_grants`. Read through the factory — never a raw system-DB connection — so the projection hits the active backend (Postgres when configured) instead of the frozen DuckDB system file:

   ```python
   def _dataset_blocks() -> list[Block]:
       from src.repositories import table_registry_repo

       blocks: dict[str, Block] = {}
       for row in table_registry_repo().list_all():
           bucket = row.get("bucket") or "(no bucket)"
           block = blocks.setdefault(bucket, {"id": bucket, "name": bucket, "items": []})
           block["items"].append({
               "resource_id": f"{bucket}.{row['name']}",
               "name": row["name"],
               "description": row.get("description"),
           })
       return list(blocks.values())
   ```

3. Register a `ResourceTypeSpec` in `RESOURCE_TYPES`. The dataclass requires `list_blocks` so the type checker will catch a missing delegate:

   ```python
   RESOURCE_TYPES[ResourceType.DATASET] = ResourceTypeSpec(
       key=ResourceType.DATASET,
       display_name="Datasets",
       description="A table available in the analytics catalog.",
       id_format="<bucket>.<table_name>",
       list_blocks=_dataset_blocks,
   )
   ```

Then wire your endpoints with `require_resource_access(ResourceType.DATASET, "{bucket}.{table}")`.

No DB migration, no startup hook, no second wiring step in `access-overview` — the registry drives both `/api/admin/resource-types` (UI dropdown) and `/api/admin/access-overview` (resource tree).

---

## Group membership sources

Members are added to groups by three sources, distinguished by the `source` column:

- **`google_sync`** — written by the OAuth callback on every login. The previous Google-sync set is wholesale replaced (DELETE + INSERT) so a removed Workspace membership disappears immediately.
- **`admin`** — written by admin actions in the UI (`/admin/access`), CLI (`agnes admin group add-member …`), or REST (`POST /api/admin/groups/{id}/members`). Survives Google sync. Admin can only delete admin-source rows.
- **`system_seed`** — written at deploy time (the `SEED_ADMIN_EMAIL` → Admin-group binding) **and** at every new-user creation (the Everyone auto-grant, issue #748 — every creation path: Google OAuth first sign-in, `POST /auth/bootstrap`, admin `POST /api/users`, marketplace import stubs — unless `AGNES_GROUP_EVERYONE_EMAIL` maps Everyone to a Workspace group instead, in which case Everyone comes exclusively from `google_sync`). The Everyone grant fires once, at creation time, and is never re-asserted afterward — an admin who later removes a user from Everyone stays removed on their next login/boot.

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
agnes admin group list
agnes admin group create Engineering --description "Eng team"
agnes admin group delete Engineering
agnes admin group members Engineering
agnes admin group add-member Engineering alice@example.com
agnes admin group remove-member Engineering alice@example.com

agnes admin grant resource-types
agnes admin grant create Engineering marketplace_plugin foundry-ai/metrics-plugin
agnes admin grant list --type marketplace_plugin
agnes admin grant list --group Engineering
agnes admin grant delete <grant-id>
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

## PAT lifetime & renewal

`agnes auth login` (browser loopback flow) mints a 90-day personal access
token (PAT). Two options were on the table for keeping analysts signed in
without re-authenticating constantly:

1. A refresh-token grant (new server primitive: a long-lived refresh
   secret that mints short-lived access tokens).
2. **Proactive re-mint** (chosen) — keep the 90-day, individually revocable
   PAT as the only credential; have the CLI remind the analyst to re-run
   `agnes auth login` before it expires.

Option 2 ships: it needs no new server-side grant type, no new secret class
to protect, and no new revocation surface — the existing PAT list/revoke
API (`agnes auth token list` / `revoke`, `/me/profile`) already covers it.
The tradeoff is a small UX cost (an occasional re-login) in exchange for
not introducing a longer-lived secret than the 90-day PAT already is.

Mechanically: Agnes PATs are HS256 JWTs, so the `exp` claim is
client-decodable without the signing secret (which never leaves the
server). `cli/token_status.py` decodes it locally and prints a one-line
stderr nudge on non-quiet commands once the token is within
`AGNES_TOKEN_RENEW_DAYS` (default 7 days; `0` disables) of expiring, at
most once per UTC day. `agnes auth whoami` shows the same expiry
year-round; `agnes update`'s convergence report carries a `token` stage
(status `ok` / `renew-soon` / `skipped`) for the same info without ever
prompting from the unattended SessionStart hook. Renewal is just
`agnes auth login` again — it overwrites the stored token in place.

No server change was needed for this: no new grant type, no PAT default
TTL change. See [`docs/HEADLESS_USAGE.md`](./HEADLESS_USAGE.md#renewal-interactive-analysts).

---

## Bootstrapping the first admin

`SEED_ADMIN_EMAIL` (env var, set by the infra Terraform module) points at the operator's email. The app startup hook in `app/main.py`:

1. Creates a `users` row for that email if missing (with `password_hash` from `SEED_ADMIN_PASSWORD` if provided).
2. Adds an Admin-group membership with `source='system_seed'`.

The hook is idempotent — re-running deploy does not duplicate or revoke. To add additional initial admins post-deploy, log in as the seed admin and use `/admin/access` or `agnes admin group add-member Admin <email>`.

---

## Migration from v9–v12 (schema v13 cutover)

The v12→v13 migration is a single-step hard cutover. The Python helper `_v12_to_v13_finalize` runs after the new tables are created and:

1. Seeds Admin/Everyone in `user_groups` (idempotent).
2. Backfills `user_group_members` from `users.groups` JSON with `source='google_sync'`.
3. Promotes every `core.admin` user-role grant to Admin-group membership with `source='system_seed'`.
4. Adds Everyone-group membership for every existing user.
5. Translates `plugin_access` rows to `resource_grants` of type `marketplace_plugin`, resource_id `<marketplace>/<plugin>`.
6. Drops `plugin_access`, `user_role_grants`, `group_mappings`, `internal_roles` (FK-correct order).
7. Drops the `users.groups` JSON column. The legacy `users.role` column is kept NULL'd as an artifact (DuckDB historical FK constraints sometimes block DROP COLUMN; the field carries no semantic meaning post-v13).

No dual-write window. Either the schema is on v12 (old code) or v13 (new code).

---

## Schema v49 — `requirement` enum + new resource types

Schema v49 (unified Browse + My Stack for Data Packages and Memory):

- `resource_grants` gains a `requirement VARCHAR DEFAULT 'available'` column. Enum: `'available'` | `'required'`. Applies to `data_package`, `memory_domain`, and `memory_item` grants. Per-group decision: same resource can be Required for Sales but Available for Engineering without duplicating the resource itself.
- New resource types in `app.resource_types.ResourceType`:
  - `DATA_PACKAGE` — admin-curated bundle of tables (`data_packages` table; M:N to `table_registry` via `data_package_tables`). Effective `TABLE` set for a user = `(direct TABLE grants) ∪ (tables in DATA_PACKAGE grants the user has)`.
  - `MEMORY_ITEM` — per-group item-level Required override. Default for an item comes from `knowledge_items.is_required` flag; a `MEMORY_ITEM` grant flips that for the specified group.
- `MEMORY_DOMAIN` grants migrated from slug strings to `memory_domains.id` references. Orphan grants (pointing at non-existent domains) preserved for admin cleanup.
- Marketplace plugins: v49 originally left them out (`marketplace_plugins.is_system` was the only mandatory path), but the tier now applies to `marketplace_plugin` grants too — `resolve_user_marketplace` serves `granted ∩ (subscribed ∪ required)`, so a required grant puts the plugin in every group member's served set without a subscription row (unsubscribe/uninstall return 409). `is_system` remains the *global* (all-users) mandatory flag; `requirement='required'` is the *group-scoped* one.

Effective Required = OR across grants. Any grant with `requirement='required'` wins for the user.

**Auto-membership stack model** (superseding the original opt-in "subscribe to add" design above): for `data_package`/`memory_domain` grants, `StackResolver.stack()` puts BOTH `required` and `available` grants automatically in the user's stack — no subscription needed for visibility or server-side query authorization. `user_stack_subscriptions` keeps its schema but is reinterpreted: a row now means "keep a local copy" (`agnes pull` downloads it), not "is in the stack". Consequently a `required → available` downgrade on `PUT /api/admin/grants/{id}` no longer needs to eagerly materialize subscription rows for `data_package`/`memory_domain` — the resource stays in every granted user's stack regardless, only the "always downloaded" guarantee relaxes to "downloaded once subscribed". `marketplace_plugin` grants are the one exception: they still resolve via `resolve_user_marketplace` (`granted ∩ (subscribed ∪ required)`) off the separate opt-out `user_plugin_optouts` table, so a `required → available` downgrade there still eagerly fans out to keep every group member's served set from silently shrinking.

## Schema v14 — FK constraints

The v13→v14 migration adds DuckDB foreign-key constraints to `user_group_members` and `resource_grants`:

- `user_group_members.group_id` → `user_groups.id` (ON DELETE CASCADE)
- `resource_grants.group_id` → `user_groups.id` (ON DELETE CASCADE)

This prevents orphaned member/grant rows pointing at a deleted group. The migration uses RENAME → CREATE-with-FK → INSERT → DROP, wrapped in `BEGIN TRANSACTION` so a partial failure rolls back without leaving the DB at a half-applied schema.

No semantic changes — v14 is backward compatible with v13 application code.
