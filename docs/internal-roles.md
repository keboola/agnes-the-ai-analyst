# Internal roles + external group mapping

Two-layer authorization model for Agnes:

- **External groups** — Cloud Identity / Google Workspace groups, pulled at sign-in into `session.google_groups`. Owned by the organization; Agnes only reads them. See `docs/auth-groups.md`.
- **Internal roles** — Agnes-defined capabilities (e.g. `context_admin`, `agent_operator`, `dataset_finance_reader`). Owned by Agnes. Registered in code by module authors, persisted in the `internal_roles` table.
- **Group mappings** — admin-managed many-to-many table binding external group IDs to internal role keys. The resolver joins this table at sign-in and writes the resolved role keys into `session["internal_roles"]`.

Permission checks read off the session — no DB hit per request.

## When to use which

| You want to gate on … | Use … |
|---|---|
| "Is this user signed in at all?" | `Depends(get_current_user)` |
| "Coarse global role" (admin / analyst / viewer) | `Depends(require_admin)` / `Depends(require_role(Role.ANALYST))` — `users.role` column |
| "Specific module capability" | `Depends(require_internal_role("context_admin"))` — this doc |

`users.role` stays the coarse gate for "may enter the building"; internal roles are the fine-grained per-module capabilities layered on top.

## Module-author workflow (registering a role)

In your module's import path (e.g. `services/context_engineering/__init__.py`):

```python
from app.auth.role_resolver import register_internal_role

register_internal_role(
    "context_admin",
    display_name="Context Engineering Admin",
    description="Manages prompt templates and retrieval settings.",
    owner_module="context_engineering",
)
```

Constraints on `key`:
- lower_snake_case, starts with a letter, ≤ 64 chars (`^[a-z][a-z0-9_]{0,63}$`)
- **immutable** — referenced from code; renaming would silently break every existing mapping. Pick carefully.
- registering the same key twice with the **same** fields is a no-op (re-import safe); registering with **different** fields raises `ValueError`. If two modules collide, one of them must rename.

`register_internal_role` only populates the in-process registry. The startup hook in `app/main.py` calls `sync_registered_roles_to_db(conn)` to reconcile the registry into the `internal_roles` table:
- **Inserts** keys that don't exist yet
- **Updates** `display_name` / `description` / `owner_module` when they've drifted from code
- **Never deletes** — a role disappearing from code (module unloaded) keeps its DB row and any mappings until an admin explicitly removes it

## Admin workflow (mapping external → internal)

Until the management UI ships, mappings are created via repository directly:

```python
from src.db import get_system_db
from src.repositories.group_mappings import GroupMappingsRepository
from src.repositories.internal_roles import InternalRolesRepository
import uuid

conn = get_system_db()
role = InternalRolesRepository(conn).get_by_key("context_admin")
GroupMappingsRepository(conn).create(
    id=str(uuid.uuid4()),
    external_group_id="engineering@example.com",  # Cloud Identity group ID
    internal_role_id=role["id"],
    assigned_by="admin@example.com",
)
conn.close()
```

After the mapping is created, affected users must **sign out and back in** for the resolver to pick it up — same refresh semantics as Google's group cache.

## Permission check (callsite)

```python
from fastapi import Depends
from app.auth.role_resolver import require_internal_role

@router.post("/context/templates")
async def update_template(
    body: TemplateUpdate,
    user: dict = Depends(require_internal_role("context_admin")),
):
    ...
```

The dependency reads `session["internal_roles"]` (populated at sign-in); a missing role returns `403 Requires internal role 'context_admin'`. Unauthenticated requests still get `401` from the upstream `get_current_user` dependency.

## Local development

`LOCAL_DEV_GROUPS` mocks `session.google_groups` (see `docs/auth-groups.md` → *Local-dev mock*). The dev-bypass branch in `app/auth/dependencies.py` re-runs the resolver every time the mocked groups change, so editing `LOCAL_DEV_GROUPS` + hitting any auth-required endpoint refreshes `session["internal_roles"]` on the next request — no need to bounce the app.

Typical dev setup:

```bash
export LOCAL_DEV_MODE=1
export LOCAL_DEV_GROUPS='[{"id":"engineering@example.com","name":"Engineering"}]'
# Register the role + create the mapping (one-shot script or manual SQL),
# then hit any protected endpoint — dev user now holds context_admin.
```

## PAT and headless requests

Internal roles are **session-scoped only**. Personal Access Tokens (PAT) and other Bearer-token clients carry a JWT that proves identity but not a signed session cookie, so `session["internal_roles"]` is never populated for them. Concretely: any endpoint protected by `Depends(require_internal_role(…))` will return `403` for a PAT client even when the corresponding user's external groups would map to that role through a browser sign-in.

This is intentional, not a bug — the same constraint already applies to `session.google_groups`, and PAT-issued JWTs deliberately don't snapshot that list (the user's group memberships can change after the token was issued without any way to re-sign the token). Two practical implications:

- **Don't gate PAT-callable endpoints with `require_internal_role`.** Use `users.role` (`require_admin` / `require_role(Role.ANALYST)`) for the coarse check, or check the JWT claims directly. Internal roles fit OAuth-flow consumers (the web UI) and the dev bypass.
- **If you need a CLI/script to act with elevated capability**, the current escape valves are: (a) issue the PAT to a user whose `users.role` already covers it, (b) call the endpoint through the OAuth flow from a browser session, or (c) wait for the planned `da admin grant-role` CLI helper (see *Future work*) which will store an explicit per-user grant outside the group-mapping flow.

## Resolution timing

Resolver runs at sign-in only:
- Google OAuth callback (`app/auth/providers/google.py`) — after `_fetch_google_groups`, before issuing the JWT
- Dev-bypass branch (`app/auth/dependencies.py`) — when `LOCAL_DEV_GROUPS` value changes for a session

Per-request reads are off `session["internal_roles"]` only; no DB hit. Trade-off: a user with a stale session keeps stale roles until they log out + back in. Same as `session.google_groups`. Cheaper than per-request DB lookup; matches the existing mental model.

## Future work (not in this PR)

- Admin UI under `/admin/role-mapping` — list registered roles + their current mappings, add/remove mappings, surface drift between registry and DB.
- Audit-log entries for mapping create/delete (write into `audit_log` with `action="role_mapping.created"` / `"role_mapping.deleted"`, `resource=f"mapping:{id}"`).
- Optional CLI helper `da admin grant-role <user-email> <role-key>` for ad-hoc grants without going through external groups.
