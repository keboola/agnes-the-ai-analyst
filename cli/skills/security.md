# Security — RBAC, permissions, and audit

## Roles
| Role | Permissions |
|------|-------------|
| `viewer` | Read catalog, view profiles, browse corporate memory |
| `analyst` | + sync data, run queries, vote, run/deploy scripts |
| `admin` | + manage users, approve knowledge, trigger sync |
| `km_admin` | + corporate memory governance |

## Managing Users
```bash
da admin add-user user@company.com --role analyst
da admin list-users
da admin remove-user <user-id>
```

## Dataset Permissions
Admins grant dataset access per user. Users can only sync datasets they have access to.

## Audit Trail
Every API call is logged. Query with:
```bash
da query "SELECT * FROM system.audit_log ORDER BY timestamp DESC LIMIT 20" --remote
```

## Script Sandboxing
User scripts run in isolated subprocess with:
- Limited environment (no access to secrets)
- Timeout (default 5 min)
- Blocked imports (subprocess, shutil, ctypes)
- Stdout/stderr size cap (64KB)

## JWT Tokens
- Session tokens: issued on interactive login (`da login`), valid 24 hours.
- For long-lived CLI / CI use, create a Personal Access Token via the UI
  (`/profile` → Personal access tokens) or CLI (`da auth token create`).
- PATs are revocable and auditable; session tokens are not.
- Contains: user_id, email, role
- Set JWT_SECRET_KEY in .env (min 32 chars)
