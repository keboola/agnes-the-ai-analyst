# Internal roles + external group mapping

Three-layer authorization model for Agnes (v9):

- **External groups** — Cloud Identity / Google Workspace groups, pulled at sign-in into `session.google_groups`. Owned by the organization; Agnes only reads them. See `docs/auth-groups.md`.
- **Internal roles** — Agnes-defined capabilities (e.g. `core.admin`, `context_engineering.admin`, `corporate_memory.curator`). Owned by Agnes. Either seeded for the platform (the `core.*` hierarchy that maps onto the legacy four-value `users.role` enum) or registered in code by module authors and persisted in the `internal_roles` table.
- **Two paths to grant a role to a user:**
  - **Group mappings** — admin-managed many-to-many table binding external group IDs to internal role keys. Resolver joins this table at sign-in and writes the resolved keys into `session["internal_roles"]`. Drives the OAuth flow.
  - **Direct user grants** (`user_role_grants`) — admin-issued or auto-seeded grants tying one user to one role. Persists across sessions; works for PAT and other headless callers where the session cache is unreachable.

`require_internal_role(...)` checks both paths: session cache first (cheap), DB-backed grants second (one query, fallback for PAT). Implies hierarchy is expanded after union — a single `core.admin` grant satisfies any check for `core.km_admin`, `core.analyst`, or `core.viewer`.

## When to use which

| You want to gate on … | Use … |
|---|---|
| "Is this user signed in at all?" | `Depends(get_current_user)` |
| "Coarse global level" (admin / km_admin / analyst / viewer) | `Depends(require_admin)` / `Depends(require_role(Role.ANALYST))` — thin wrappers over `require_internal_role(f"core.{role}")` |
| "Specific module capability" | `Depends(require_internal_role("corporate_memory.curator"))` |

`require_admin` and `require_role` are convenience helpers — they're the same gate as `require_internal_role("core.admin")` / `require_internal_role(f"core.{role.value}")` underneath. Use them for "is this user at least an analyst" type checks where the implies hierarchy carries the meaning. Use `require_internal_role` directly for fine-grained module capabilities.

## Role-key naming convention

Role keys follow `<owner_module>.<capability>` — lower-snake-case segments separated by dots, total length up to 64 characters, regex `^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$`. Examples:

| Key | Owner module | Notes |
|---|---|---|
| `core.viewer` / `core.analyst` / `core.km_admin` / `core.admin` | `core` | Seeded by the platform; `is_core=true`. Map onto legacy `users.role` enum. Hierarchy via `implies`. |
| `context_engineering.admin` | `context_engineering` | Module-author registered. Manage prompt templates and retrieval settings. |
| `corporate_memory.curator` | `corporate_memory` | Module-author registered. Manage memory items and verification evidence. |

Constraints on `key`:
- Lower-snake-case segments, dots between segments. The first character of each segment must be a letter.
- Total length ≤ 64 chars.
- **Immutable** — referenced from code; renaming would silently break every existing mapping or grant. Pick carefully.
- Registering the same key twice with the **same** fields is a no-op (re-import safe); registering with **different** fields raises `ValueError`. If two modules collide, one of them must rename.

## Implies hierarchy

`internal_roles.implies` is a JSON array of role keys this role transitively grants. The seed populates:

```text
core.admin     →  ["core.km_admin"]
core.km_admin  →  ["core.analyst"]
core.analyst   →  ["core.viewer"]
core.viewer    →  []
```

`expand_implies(role_keys, conn)` does BFS from the input keys and returns the deduped, sorted closure. So `expand_implies(["core.admin"], conn)` returns `["core.admin", "core.analyst", "core.km_admin", "core.viewer"]` and `require_internal_role("core.viewer")` succeeds for any user holding any core.* role.

Module-author roles can declare their own implies — useful when one module-level capability is a strict superset of another. Modules must not point implies at `core.*` keys (that would let any module-role expand to platform-admin); validation lives in `register_internal_role`.

## Module-author workflow (registering a role)

In your module's import path (e.g. `services/context_engineering/__init__.py`):

```python
from app.auth.role_resolver import register_internal_role

register_internal_role(
    "context_engineering.admin",
    display_name="Context Engineering Admin",
    description="Manages prompt templates and retrieval settings.",
    owner_module="context_engineering",
)
```

`register_internal_role` only populates the in-process registry. The startup hook in `app/main.py` calls `sync_registered_roles_to_db(conn)` to reconcile the registry into the `internal_roles` table:

- **Inserts** keys that don't exist yet.
- **Updates** `display_name` / `description` / `owner_module` when they've drifted from code.
- **Never deletes** — a role disappearing from code (module unloaded) keeps its DB row and any mappings/grants until an admin explicitly removes it.

## Admin workflows

### Via the admin UI (preferred for one-offs)

Navigate to **Admin → Role mapping** (`/admin/role-mapping`) for the group→role bindings. The page shows:

- **Internal roles** table (read-only): every registered role + how many mappings/grants reference it.
- **Group → role mappings** table with delete buttons + a create form.

For per-user grants, open the user's detail page (Admin → Users → click a user). Three sections:

- **Core role** — single-select (`viewer/analyst/km_admin/admin`); changes update the user's core.* grant.
- **Additional capabilities** — multi-checkbox of non-core internal roles; toggle on/off.
- **Effective roles (debug)** — shows direct grants, group-derived grants, and the expanded set after implies BFS. Useful for "why does this user have access?" investigations.

### Via the CLI (preferred for scripts and CI)

```bash
da admin role list                                  # all internal roles
da admin role show core.admin                       # one role + counts
da admin mapping list                               # all group → role bindings
da admin mapping create engineering@example.com core.km_admin
da admin mapping delete <mapping-id>
da admin grant-role alice@example.com core.admin    # direct grant
da admin revoke-role alice@example.com core.admin
da admin effective-roles alice@example.com          # debug: direct + group + expanded
```

All `da admin` subcommands hit the REST API and authenticate via PAT — works headlessly without a browser session. The PAT-aware path in `require_internal_role` (DB lookup over `user_role_grants`) makes this work; without that, every CLI admin command would 403.

### Via the REST API (for custom tooling)

All endpoints under `/api/admin`, gated by `require_internal_role("core.admin")`:

| Endpoint | Purpose |
|---|---|
| `GET /internal-roles` | list all roles |
| `GET /group-mappings` | list group bindings |
| `POST /group-mappings` | create binding (`{external_group_id, role_key}`) |
| `DELETE /group-mappings/{id}` | remove binding |
| `GET /users/{id}/role-grants` | list direct grants for a user |
| `POST /users/{id}/role-grants` | grant a role (`{role_key}`) |
| `DELETE /users/{id}/role-grants/{grant_id}` | revoke a direct grant |
| `GET /users/{id}/effective-roles` | debug view (direct + group + expanded) |

Audit log entries are written for every mutation (`role_mapping.created/deleted`, `role_grant.created/deleted`).

## Refresh semantics

Two scenarios:

**Group mappings change** — affected users see the new resolution on next sign-in. Their session cache holds the old set until logout. If you can't get the user to log out (long-lived session, automated client), `Admin → Users → deactivate then reactivate` invalidates the existing session and forces a fresh sign-in on the next request.

**Direct grants change** — take effect on the **very next request**, no logout needed. The DB-backed fallback in `require_internal_role` consults `user_role_grants` per request when the session cache doesn't already grant access, so an admin can grant `core.admin` to an active user and they'll see admin endpoints immediately.

This asymmetry is intentional: group mappings are cached because they're the high-volume hot path; direct grants are admin-issued and rare, so the per-request DB lookup is acceptable.

## Permission check (callsite)

```python
from fastapi import Depends
from app.auth.role_resolver import require_internal_role

@router.post("/context/templates")
async def update_template(
    body: TemplateUpdate,
    user: dict = Depends(require_internal_role("context_engineering.admin")),
):
    ...
```

The dependency reads `session["internal_roles"]` first; on miss, falls back to a DB lookup against `user_role_grants` for the authenticated user, expanding implies. A 403 is raised only when neither path produces the required role. Unauthenticated requests still get 401 from the upstream `get_current_user` dependency.

## Local development

`LOCAL_DEV_GROUPS` mocks `session.google_groups` (see `docs/auth-groups.md` → *Local-dev mock*). The dev-bypass branch in `app/auth/dependencies.py` re-runs the resolver every time the mocked groups change. Direct grants for the dev user can be created via the CLI, REST API, or by inserting `user_role_grants` rows in DuckDB directly.

Typical dev setup:

```bash
export LOCAL_DEV_MODE=1
export LOCAL_DEV_GROUPS='[{"id":"engineering@example.com","name":"Engineering"}]'
# Either: register a mapping (Admin UI / da admin mapping create / direct INSERT)
# Or: grant directly (da admin grant-role dev@localhost <role-key>)
# Then hit any protected endpoint — dev user holds the role on the next request.
```

## PAT and headless requests

PATs and other Bearer-token clients carry a JWT that proves identity but not a signed session cookie, so `session["internal_roles"]` is never populated for them. **However**, v9 `require_internal_role` falls back to `user_role_grants` in the database on session-cache miss, so a PAT client whose user has a matching direct grant succeeds normally. This makes admin CLIs work uniformly via PAT.

Group memberships still don't apply to PAT (the JWT doesn't snapshot them), so a user who would get `core.admin` via a Cloud Identity group mapping in their browser session will need a direct grant to use that capability via PAT. The CLI `da admin grant-role <email> <role-key>` is the supported way to issue such grants.

## Resolution timing

| Source | When | Cost |
|---|---|---|
| Group mappings | At sign-in (Google OAuth callback + dev-bypass when `LOCAL_DEV_GROUPS` changes) | One DB query at login; cached on session for the session lifetime. |
| Direct grants | Per-request fallback inside `require_internal_role` when the session doesn't already grant the role | One DB query per gated request that wasn't satisfied by the cache. Acceptable for admin/CLI traffic; not used for high-volume per-user-data endpoints. |

Trade-off: a user with a stale session keeps stale group-resolved roles until they log out + back in. Direct grants take effect immediately. Same as `session.google_groups`. Cheaper than per-request resolution everywhere; matches the existing mental model.

## Migration notes (v8 → v9)

- v9 schema seeds `core.*` rows (`is_core=true`) with the legacy hierarchy in `implies`.
- For each existing user with a non-null `users.role`, the migration inserts one `user_role_grants` row pointing at `core.{role}` (`source='auto-seed'`).
- The legacy `users.role` column is then NULL-ed (DuckDB rejects DROP COLUMN while a FK references the table; physical drop deferred).
- `UserRepository.create()` and `update()` write to both `users.role` (legacy compat for in-flight code) and `user_role_grants` (new source of truth) so a deployment that lands mid-flight stays consistent.

## Future work (not in 0.11.4)

- Optional CLI helper `da admin grant-role` is shipped; the analogous `da admin set-core-role <user-email> <role>` (single-step replace) could trim two-step "revoke old + grant new" patterns.
- Cleanup migration to physically DROP the legacy `users.role` column once we either rebuild the table (CREATE NEW + COPY + DROP OLD) or DuckDB ALTER w/ FK support stabilizes.
- Fine-grained audit history (who held what when) — currently the audit log only records mutations, not the historical state.
