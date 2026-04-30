# Security — RBAC, permissions, and audit

## Access control (v19+)

Two layers, no role hierarchy:

- **`Admin` system group** — god-mode for app-level actions. Members can do anything.
- **`resource_grants(group, resource_type, resource_id)`** — per-group grants on individual resources (tables, marketplace plugins, memory domains).

Every non-admin access requires an explicit grant. There is no `is_public` shortcut and no implicit Everyone fallback — admins curate access by minting grants on groups the user belongs to.

## Managing users

```bash
da admin add-user user@company.com           # creates a non-admin user
da admin list-users
da admin remove-user <user-id>
```

Admin promotion is a separate action — there is no `--role admin` flag. Add the user to the `Admin` system group:

```bash
da admin group list                          # find the Admin group id
da admin group add-member <admin-group-id> user@company.com
```

Removed in v19: `da admin set-role` (use group memberships instead). Old call sites hard-fail with a replacement command in the error message.

## Granting table access

```bash
da admin grant resource-types                # see registered resource types
da admin grant create --group <id> --type table --id <table-id>
da admin grant list
da admin grant delete <grant-id>
```

The web UI at `/admin/access` shows the same model: groups on the left, grantable resources on the right, per-row checkboxes plus per-block "Grant all" / "Revoke all" bulk actions.

## Audit trail

Every API mutation is logged:

```bash
da query "SELECT * FROM system.audit_log ORDER BY timestamp DESC LIMIT 20" --remote
```

## Script sandboxing

User scripts run in an isolated subprocess with:

- Limited environment (no access to secrets)
- Timeout (default 5 min)
- Blocked imports (subprocess, shutil, ctypes)
- Stdout/stderr size cap (64KB)

## JWT tokens

- Session tokens: issued on interactive login (`da login`), valid 24 hours.
- For long-lived CLI / CI use, create a Personal Access Token via the UI (`/tokens` → New token) or CLI (`da auth token create`).
- PATs are revocable and auditable; session tokens are not.
- Claims: `sub` (user_id), `email`, `typ`, `exp`, `jti`. **No `role` claim** — admin status derives from `user_group_members` at request time via `app.auth.access.is_user_admin`.
- Set `JWT_SECRET_KEY` in `.env` (min 32 chars).
