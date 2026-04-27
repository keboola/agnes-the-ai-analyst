# Internal roles + external group mapping

Three-layer authorization model for Agnes (v9):

- **External groups** — Cloud Identity / Google Workspace groups, pulled at sign-in into `session.google_groups`. Owned by the organization; Agnes only reads them. See `docs/auth-groups.md`.
- **Internal roles** — Agnes-defined capabilities (e.g. `core.admin`, `context_engineering.admin`, `corporate_memory.curator`). Owned by Agnes. Either seeded for the platform (the `core.*` hierarchy that maps onto the legacy four-value `users.role` enum) or registered in code by module authors and persisted in the `internal_roles` table.
- **Two paths to grant a role to a user:**
  - **Group mappings** — admin-managed many-to-many table binding external group IDs to internal role keys. Resolver joins this table at sign-in and writes the resolved keys into `session["internal_roles"]`. Drives the OAuth flow.
  - **Direct user grants** (`user_role_grants`) — admin-issued or auto-seeded grants tying one user to one role. Persists across sessions; works for PAT and other headless callers where the session cache is unreachable.

`require_internal_role(...)` checks both paths: session cache first (cheap), DB-backed grants second (one query, fallback for PAT). Implies hierarchy is expanded after union — a single `core.admin` grant satisfies any check for `core.km_admin`, `core.analyst`, or `core.viewer`.

---

## Quickstart by role

### "I'm an operator and I want to grant someone admin"

Three options, pick whichever fits the situation:

| Situation | Tool | Steps |
|---|---|---|
| One-off, you're at a browser | Admin UI | **Admin → Users → click the target → set Core role to "Admin"** (or check additional capabilities). Last-admin protection blocks the obvious foot-gun. |
| Bulk / scripted / from CI | CLI | `da admin grant-role <email> core.admin` (PAT-authenticated; works headlessly). |
| Custom tooling (e.g. SCIM bridge) | REST | `POST /api/admin/users/{id}/role-grants` with `{ "role_key": "core.admin" }`. Bearer-token auth. |

For **group-based** access (everyone in `engineering@acme.com` should be analyst), use **Admin → Role mapping** and click the matching chip in *Known groups* — it pre-fills the form. The mapping takes effect on the **next sign-in** of affected users, not retroactively.

### "I'm a user and I want to know what I have access to"

Open `/profile`. Three role-related sections render server-side:

- **Effective roles** — chip cloud of every internal-role key you currently hold (after implies expansion). This is what the auth gate sees.
- **Direct grants** — rows in `user_role_grants` with their `source` label (`auto-seed` for v8 backfill, `direct` for explicit admin grants).
- **Roles via groups** — for each Cloud Identity group you're a member of, the role(s) it grants you via the admin's `group_mappings` table.

If you're missing a role you expect, the breakdown tells you whether the gap is on the *grant* side (no row in `user_role_grants`) or the *mapping* side (your group isn't bound to a role yet) — actionable info to take to your admin. Admins additionally see a deep-link to `/admin/users/{id}` for self-managing their own grants.

### "I'm a module author and I need to gate my endpoints"

See [Module-author workflow](#module-author-workflow-step-by-step) below for the full walkthrough. TL;DR:

```python
# 1. Pick a key in your module's namespace, register it at import time.
from app.auth.role_resolver import register_internal_role
register_internal_role(
    "context_engineering.admin",
    display_name="Context Engineering Admin",
    description="Manages prompt templates and retrieval settings.",
    owner_module="context_engineering",
)

# 2. Gate your endpoint.
from app.auth.role_resolver import require_internal_role
@router.post("/context/templates")
async def update_template(
    user: dict = Depends(require_internal_role("context_engineering.admin")),
):
    ...
```

The `register_*` call runs at module import; `sync_registered_roles_to_db` (called by `app/main.py` at startup) reconciles the registry into the DB. Admins then see your role on `/admin/role-mapping` and can bind groups to it or grant individual users.

---

## When to use which gate

| You want to gate on … | Use … |
|---|---|
| "Is this user signed in at all?" | `Depends(get_current_user)` |
| "Coarse global level" (admin / km_admin / analyst / viewer) | `Depends(require_admin)` / `Depends(require_role(Role.ANALYST))` — thin wrappers over `require_internal_role(f"core.{role}")` |
| "Specific module capability" | `Depends(require_internal_role("corporate_memory.curator"))` |

`require_admin` and `require_role` are convenience helpers — they're the same gate as `require_internal_role("core.admin")` / `require_internal_role(f"core.{role.value}")` underneath. Use them for "is this user at least an analyst" type checks where the implies hierarchy carries the meaning. Use `require_internal_role` directly for fine-grained module capabilities.

**Don't gate on `user.get("role") == "admin"` directly** — that's the legacy column, kept only as a deprecated artifact. Use the helpers above. They route through the resolver and stay correct after grant revocations; the legacy column is a stale snapshot the role-management endpoints don't touch (see [Hydration shim](#hydration-shim) below for why a sweeping read of `user["role"]` keeps working anyway).

---

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
- **First segment ⇔ owner_module convention**: `corporate_memory.curator` is owned by `corporate_memory`. The mapping UI uses this prefix to group your role with other roles your module registers.
- **Don't re-use `core.*`** — that namespace is reserved for the seeded platform hierarchy. Pick a module-prefixed key even if it overlaps semantically with a core role (e.g. `corporate_memory.admin`, not `core.curator`).

---

## Implies hierarchy

`internal_roles.implies` is a JSON array of role keys this role transitively grants. The seed populates:

```text
core.admin     →  ["core.km_admin"]
core.km_admin  →  ["core.analyst"]
core.analyst   →  ["core.viewer"]
core.viewer    →  []
```

`expand_implies(role_keys, conn)` does BFS from the input keys and returns the deduped, sorted closure. So `expand_implies(["core.admin"], conn)` returns `["core.admin", "core.analyst", "core.km_admin", "core.viewer"]` and `require_internal_role("core.viewer")` succeeds for any user holding any core.* role.

**`implies` is currently seeded only for the `core.*` hierarchy** (via `_seed_core_roles` in `src/db.py`). The `register_internal_role` API accepts `display_name`, `description`, and `owner_module` — there is no `implies=` keyword argument today, so module authors cannot declare in-namespace hierarchies through the registry. The closure expansion (`expand_implies`) reads whatever `implies` JSON sits in the row, so the *runtime* honors it; what's missing is the registry-side write path.

If your module needs a hierarchy (e.g. `editor` ⊆ `admin`), gate on each level explicitly today:

```python
# Today — register both levels independently, gate per level.
register_internal_role(
    "context_engineering.editor",
    display_name="Context Engineering Editor",
    description="Save drafts.",
    owner_module="context_engineering",
)
register_internal_role(
    "context_engineering.admin",
    display_name="Context Engineering Admin",
    description="Save and ship.",
    owner_module="context_engineering",
)

# Endpoint-level: the admin gate is its own dependency. If you want the
# admin to satisfy editor checks too, grant them both roles in
# user_role_grants (or bind both via group_mappings) — until module-level
# implies lands, the registry won't auto-expand for you.
@router.post("/save")
async def save(user = Depends(require_internal_role("context_engineering.editor"))): ...

@router.post("/ship")
async def ship(user = Depends(require_internal_role("context_engineering.admin"))): ...
```

A future change can extend `register_internal_role` + `InternalRoleSpec` + `sync_registered_roles_to_db` to write `implies` from code. The runtime invariant — *module-level implies must never point at `core.*`* — applies whichever side of the registry/seed boundary you're on; today it's not enforced because the field isn't exposed, but a registry-side implementation must validate it.

---

## Module-author workflow (step-by-step)

This is what every new module needs to do to plug into the role system. Five steps.

### 1. Register the role at import time

In your module's package init (e.g. `services/context_engineering/__init__.py`):

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

Nothing to do in startup code — adding the import side-effect to the module-init is enough as long as your module is imported by `app/main.py` (directly or transitively).

### 2. Gate your endpoints with `require_internal_role`

```python
from fastapi import APIRouter, Depends
from app.auth.role_resolver import require_internal_role

router = APIRouter(prefix="/api/context", tags=["context"])

@router.post("/templates")
async def update_template(
    body: TemplateUpdate,
    user: dict = Depends(require_internal_role("context_engineering.admin")),
):
    ...
```

The dependency reads `session["internal_roles"]` first (the OAuth fast path); on miss, falls back to a DB lookup against `user_role_grants` for the authenticated user, expanding implies. A 403 is raised only when neither path produces the required role. Unauthenticated requests still get 401 from the upstream `get_current_user` dependency.

### 3. Decide if you need a hierarchy

If your module has multiple capability levels (e.g. *editor* who can save drafts, *publisher* who can ship), register each level independently — the registry-side `implies` write path doesn't exist yet (see [Implies hierarchy](#implies-hierarchy) for what *is* supported in 0.11.4).

```python
register_internal_role(
    "your_module.editor",
    display_name="Your Module Editor",
    description="Save drafts.",
    owner_module="your_module",
)
register_internal_role(
    "your_module.publisher",
    display_name="Your Module Publisher",
    description="Ship drafts to production.",
    owner_module="your_module",
)
```

Until module-level `implies` lands, give a publisher both roles when granting (admin issues `your_module.editor` *and* `your_module.publisher` via the UI/CLI/REST, or binds the same Cloud Identity group to both rows in `group_mappings`). The runtime resolver will treat them as a flat union — there's no automatic "publisher ⊇ editor" until the registry side ships.

Don't manually `OR` two `require_internal_role` checks at the endpoint to fake a hierarchy — that pattern doesn't compose as you add levels. Pick a primary capability per endpoint and lean on the grants/mappings to keep "everyone with X also has Y" in sync.

### 4. Test the gate

Pattern that works across FastAPI test clients:

```python
def test_endpoint_requires_module_admin(client, fresh_db):
    # 1. Sign in a non-admin user.
    user_token = _seed_user_with_role(fresh_db, "u@t", "analyst")

    # 2. Without the module role: 403.
    resp = client.post("/api/context/templates",
                       headers={"Authorization": f"Bearer {user_token}"},
                       json={...})
    assert resp.status_code == 403
    assert "context_engineering.admin" in resp.json()["detail"]

    # 3. Grant the role directly, retry: 200.
    _grant_role(fresh_db, "u@t", "context_engineering.admin")
    resp = client.post("/api/context/templates",
                       headers={"Authorization": f"Bearer {user_token}"},
                       json={...})
    assert resp.status_code == 200
```

For unit tests of business logic that don't go through HTTP, mock `_get_internal_role_keys` or set `session["internal_roles"]` directly — but always include an end-to-end test that exercises the real gate.

### 5. Document the role for admins

The `display_name` and `description` you pass to `register_internal_role` show up on `/admin/role-mapping`. Write the description from the **admin's** point of view: "*Manages prompt templates and retrieval settings*", not "*Allows write access to context_engineering tables*". Admins are deciding which Cloud Identity groups to bind to your role and which users to grant it to — they need the capability framing, not the implementation.

If your module ships with sensible defaults (e.g. "everyone in `engineering@` should automatically be `your_module.editor`"), document the recommended group mapping in your module's README. Don't hardcode it — admins always make the binding decision.

---

## Admin workflows (full reference)

### Via the admin UI (preferred for one-offs)

**Admin → Role mapping** (`/admin/role-mapping`):

- **Internal roles** table (read-only): every registered role + how many mappings/grants reference it. Module-author roles are visually distinguished from `core.*` (different badge color, separate `is_core=false`).
- **Known groups** picker above the create-mapping form: clickable chips for the calling admin's own session groups (tagged "your group") plus any external group IDs already used in existing mappings (tagged "already mapped"). Click a chip to fill the form's group-id field. Empty-state copy points at `LOCAL_DEV_GROUPS` / Google sign-in for when you don't have anything to suggest.
- **Group → role mappings** table with delete buttons + the create form. Mappings take effect on the **next sign-in** of affected users (group resolution is cached on the session — see [Refresh semantics](#refresh-semantics)).

**Admin → Users → click a user** (`/admin/users/{id}`):

- **Core role** — single-select (`viewer/analyst/km_admin/admin`); changes update the user's core.* grant via `DELETE` + `POST` on the role-grants endpoint.
- **Additional capabilities** — multi-checkbox of non-core internal roles; toggle on/off.
- **Effective roles (debug)** — direct grants, group-derived (best-effort, only the calling admin's own groups), and the expanded set after implies BFS. Useful for *"why does this user have access?"* investigations.

Direct grants (the per-user table) take effect on the **very next request** for that user — no logout needed.

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

---

## Refresh semantics

Two scenarios:

**Group mappings change** — affected users see the new resolution on next sign-in. Their session cache holds the old set until logout. If you can't get the user to log out (long-lived session, automated client), `Admin → Users → deactivate then reactivate` invalidates the existing session and forces a fresh sign-in on the next request.

**Direct grants change** — take effect on the **very next request**, no logout needed. Two reasons:

1. The DB-backed fallback in `require_internal_role` consults `user_role_grants` per request when the session cache doesn't already grant access.
2. The `_hydrate_legacy_role` shim (see [Hydration shim](#hydration-shim) below) re-resolves on every authenticated request, so legacy `user["role"]` reads also pick up the new state immediately.

This asymmetry is intentional: group mappings are cached because they're the high-volume hot path; direct grants are admin-issued and rare, so the per-request DB lookup is acceptable.

---

## Hydration shim

The v8 → v9 migration NULL-ed the legacy `users.role` column for every existing user (DuckDB rejects DROP COLUMN under the FK reference, so the column lives on as a deprecated artifact). A long tail of read sites still inspects `user["role"]` directly — Jinja2 templates (`session.user.role`), the dashboard's `UserInfo.is_admin`, the catalog/sync admin-bypass paths in `app/api/catalog.py` and `app/api/sync.py`, and so on.

`_hydrate_legacy_role` in `app/auth/dependencies.py` runs after every authenticated user load (both `LOCAL_DEV_MODE` and JWT/PAT paths). It re-resolves the highest-level `core.*` grant from `user_role_grants` and writes it into `user["role"]` as the legacy enum string — so every old call site keeps working without a mass refactor.

**Always re-resolves, never trusts the legacy column.** The role-management endpoints (`POST/DELETE /api/admin/users/{id}/role-grants`, plus the `changeCoreRole` UI flow) modify `user_role_grants` without touching the legacy column. If the shim short-circuited on a truthy stale value, a downgraded user would keep `role="admin"` in their dict even though the grants table no longer agrees — and `_is_admin_user_dict` (in `src/rbac.py`) and the catalog/sync short-circuits would silently retain elevated access while `require_internal_role` correctly denied the API gates. The fix is to make the grants table the single source of truth on every authenticated request. Cost: one extra DB round-trip per authenticated request — same as the existing PAT-aware fallback. Worth the consistency.

---

## Local development

`LOCAL_DEV_GROUPS` mocks `session.google_groups` (see `docs/auth-groups.md` → *Local-dev mock*). The dev-bypass branch in `app/auth/dependencies.py` re-runs the resolver every time the mocked groups change, and passes the dev user's `id` so direct grants are folded into the session cache too — your seeded admin user shows up with the full `core.*` hierarchy on the first request, no DB-fallback hop per gate.

Typical dev setup:

```bash
export LOCAL_DEV_MODE=1
export LOCAL_DEV_GROUPS='[{"id":"engineering@example.com","name":"Engineering"}]'
# Either: register a mapping (Admin UI → Role mapping → Known groups picker)
# Or: grant directly (da admin grant-role dev@localhost <role-key>)
# Then hit any protected endpoint — dev user holds the role on the next request.
```

The Known-groups picker on `/admin/role-mapping` reads exactly your `LOCAL_DEV_GROUPS` IDs at page-render time, so you don't have to remember Cloud Identity opaque IDs.

---

## PAT and headless requests

PATs and other Bearer-token clients carry a JWT that proves identity but not a signed session cookie, so `session["internal_roles"]` is never populated for them. **However**, v9 `require_internal_role` falls back to `user_role_grants` in the database on session-cache miss, so a PAT client whose user has a matching direct grant succeeds normally. This makes admin CLIs work uniformly via PAT.

Group memberships still don't apply to PAT (the JWT doesn't snapshot them), so a user who would get `core.admin` via a Cloud Identity group mapping in their browser session will need a direct grant to use that capability via PAT. The CLI `da admin grant-role <email> <role-key>` is the supported way to issue such grants.

For background services (the scheduler, telegram bot, etc.) that need to call gated endpoints, mint a long-lived PAT for a service-account user via `/tokens` and pass it through env (`SCHEDULER_API_TOKEN`, etc.). The PAT carries the service account's direct grants — gate semantics are identical to a human admin's session.

---

## Resolution timing

| Source | When | Cost |
|---|---|---|
| Group mappings | At sign-in (Google OAuth callback + dev-bypass when `LOCAL_DEV_GROUPS` changes) | One DB query at login; cached on session for the session lifetime. |
| Direct grants | Folded into the session cache at sign-in alongside group mappings (after the v0.11.4 fix). Per-request DB fallback still fires for PAT/headless callers without a session, or for sessions that predate the fix. | One DB query at login; per-request fallback adds one query per gated request that wasn't already satisfied by the cache. Acceptable for admin/CLI traffic; not used for high-volume per-user-data endpoints. |

Trade-off: a user with a stale session keeps stale group-resolved roles until they log out + back in. Direct grants take effect immediately even on a stale session because the per-request fallback always re-checks the DB. Same mental model as `session.google_groups`.

---

## Migration notes (v8 → v9)

- v9 schema seeds `core.*` rows (`is_core=true`) with the legacy hierarchy in `implies`.
- For each existing user with a non-null `users.role`, the migration inserts one `user_role_grants` row pointing at `core.{role}` (`source='auto-seed'`).
- The legacy `users.role` column is then NULL-ed (DuckDB rejects DROP COLUMN while a FK references the table; physical drop deferred).
- `UserRepository.create()` and `update()` write to both `users.role` (legacy compat for in-flight code) and `user_role_grants` (new source of truth) so a deployment that lands mid-flight stays consistent.
- The role-management endpoints (`POST/DELETE` on `/api/admin/users/{id}/role-grants`) only modify `user_role_grants`. The legacy column accumulates stale values from `UserRepository.create/update`; the hydration shim ignores them and reads grants on every request — see [Hydration shim](#hydration-shim).

---

## Common pitfalls

- **Gating on the legacy enum directly** — `if user.get("role") == "admin": ...` works *today* via the hydration shim, but bypasses the resolver's implies expansion. Prefer `await require_internal_role("core.admin")` (the dependency) or `is_admin(user, conn)` (the helper from `src/rbac.py`).
- **Forgetting `owner_module` in `register_internal_role`** — the field defaults to `None`, but the admin UI uses it to group your role with other roles your module registers. Always pass it; same value as the first dot-segment of the key.
- **Pointing implies at `core.*`** — module roles must never claim platform-admin transitively. Validation rejects this; pick implies inside your own namespace.
- **Manually rewriting `users.role`** — the column is a deprecated artifact; only `UserRepository.create/update` should touch it (and only for in-flight legacy compat). Modify `user_role_grants` instead.
- **Expecting roles to refresh mid-session** — group-mapped roles are cached at sign-in. If an admin needs to revoke access *now*, deactivate-then-reactivate the user's account (forces re-sign-in) or use a direct grant change (takes effect on the next request).

---

## Future work (not in 0.11.4)

- Optional `da admin set-core-role <user-email> <role>` (single-step replace) could trim the two-step *revoke old + grant new* pattern the UI currently uses.
- Cleanup migration to physically DROP the legacy `users.role` column once we either rebuild the table (CREATE NEW + COPY + DROP OLD) or DuckDB ALTER w/ FK support stabilizes.
- Fine-grained audit history (who held what when) — currently the audit log only records mutations, not the historical state.
- Self-service "request access" flow on `/profile` — let a user click a role they don't have and ping an admin for the grant. Today the user has to ask out-of-band.
