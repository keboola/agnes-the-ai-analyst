# Google Workspace Group Sync

How Agnes pulls a user's Workspace group memberships at Google sign-in and
where they end up in the database.

## Flow at a glance

The OAuth callback in `app/auth/providers/google.py` calls
`app.auth.group_sync.fetch_user_groups(email)` and feeds the result into
`UserGroupMembersRepository.replace_google_sync_groups`, which DELETE+INSERTs
the user's `source='google_sync'` rows in `user_group_members`. Admin-added
rows (`source='admin'`) and seeded system rows (`source='system_seed'`) are
untouched.

```
Browser → /auth/google/callback
  → exchange code for ID token (email)
  → fetch_user_groups(email)        ← keyless DWD + Admin SDK groups.list
  → ensure each group in user_groups
  → replace_google_sync_groups(...)  ← per-user DELETE+INSERT, source-scoped
  → set session cookie, redirect to /dashboard
```

The fetch is **fail-soft**: any error (missing config, API 4xx/5xx, network
outage) returns `[]`, the membership snapshot from the previous login stays
intact, and the user is signed in regardless. A transient outage does not
empty a user's groups.

## How `fetch_user_groups` authenticates to Google

The function in `app/auth/group_sync.py` uses **keyless Domain-Wide
Delegation**: the VM service account signs the impersonation JWT through
the IAM `signJwt` API (no private key on disk anywhere), then exchanges
that JWT for a short-lived OAuth token scoped to
`admin.directory.group.readonly`. The Admin SDK
`groups.list?userKey=<email>` endpoint returns both static and dynamic
group memberships in one paginated call.

Two identities are involved:

- **The VM service account** (auto-detected from the GCE metadata server)
  is the issuer of the JWT. Its IAM unique ID must be allowlisted via DWD.
- **The impersonated subject** (`GOOGLE_ADMIN_SDK_SUBJECT` env var) is a
  real Workspace user with directory read privileges. The Admin SDK call
  is authorized as if that admin made it.

## GCP setup (one-off, per deployment)

1. **Enable Admin SDK API** on the project:
   ```
   APIs & Services → Library → "Admin SDK API" → Enable
   ```
2. **IAM binding on the VM SA** — grant the SA `roles/iam.serviceAccountTokenCreator`
   on itself, so it can call `IAMCredentials.signJwt`:
   ```bash
   gcloud iam service-accounts add-iam-policy-binding <sa-email> \
     --member="serviceAccount:<sa-email>" \
     --role="roles/iam.serviceAccountTokenCreator" \
     --project=<project-id>
   ```
3. **Domain-Wide Delegation** in `admin.google.com`:
   ```
   Security → API controls → Domain-wide Delegation → Add new
   Client ID:   <SA's numeric Unique ID, e.g. 103511645014740068359>
   OAuth scope: https://www.googleapis.com/auth/admin.directory.group.readonly
   ```
   The Unique ID is the field `uniqueId` returned by
   `gcloud iam service-accounts describe <sa-email>`.

This setup is per Workspace tenant. A Workspace super admin must grant
the DWD entry; project-level GCP IAM cannot do it.

## Required env on the VM

```env
GOOGLE_ADMIN_SDK_SUBJECT=admin@your-domain.com
```

The Workspace admin email the SA impersonates. **Without this, the function
fails soft and returns `[]`** — group sync is silently disabled. The admin
must already have directory read privileges in `admin.google.com`; a regular
user with no admin role will produce a `403 Not Authorized` from the Admin
SDK even with DWD in place.

## Optional env

```env
GOOGLE_ADMIN_SDK_SA_EMAIL=explicit-sa@project.iam.gserviceaccount.com
```

When unset, the SA email is auto-detected from the GCE metadata server.
Set this only when running off-VM (CI / local dev with explicit ADC) or
when impersonating a different SA than the one the VM is attached to.

## Local dev / CI mock

```env
GOOGLE_ADMIN_SDK_MOCK_GROUPS=engineers@example.com,admins@example.com
```

When set, all Google calls are bypassed and `fetch_user_groups` returns the
parsed list verbatim. Empty value (`""`) returns `[]`. Unset → real
keyless-DWD path. The mock is honoured regardless of `LOCAL_DEV_MODE` so
integration tests can exercise the full callback path with deterministic
group lists.

## Verifying the setup

After Terraform apply + subject seeded into `.env`, on the VM:

```bash
sudo docker exec agnes-app-1 python -c "
from app.auth.group_sync import fetch_user_groups
print(fetch_user_groups('user@your-domain.com'))
"
```

Expected: a Python list of group emails. `[]` means either the user has no
groups or the function fail-softed — check `docker logs agnes-app-1 | grep
"group sync\|group fetch\|Admin SDK"` for the actual reason.

Common failure modes:

- `... GOOGLE_ADMIN_SDK_SUBJECT not set; skipping group fetch` — env var
  missing.
- `... Admin SDK init failed: ...` — DWD entry missing or wrong client ID,
  Admin SDK API disabled, or `tokenCreator` IAM binding missing.
- `... Group fetch failed for X: HttpError 403 Not Authorized to access
  this resource/api` — the impersonated subject does not have directory
  read privileges in Workspace.

## Why not the simpler approaches

Earlier iterations tried two simpler paths that did not work in every
deployment:

- **User OAuth token + Cloud Identity API + `groups.security` label**.
  Worked at one tenant where every group carried the `security` label, but
  returned `403 Error 4013` at another where group label coverage differs.
  Tenant-dependent, so dropped from the codebase.
- **VM SA + Cloud Identity `searchTransitiveGroups` with admin role**.
  Requires assigning a Workspace admin role to the SA, which several
  Workspace tenants block for cross-tenant service accounts (`prj-*` SAs
  living under a different Cloud Organization than the Workspace customer
  ID). DWD is the documented way around that.

Keyless DWD is the path that works regardless of tenant configuration and
keeps zero key material on the host.
