# Google Workspace Groups in /profile

How Agnes pulls a user's group memberships at Google sign-in and where they end up.

## Google Cloud setup (per OAuth client / project)

In the GCP project hosting the OAuth client (e.g. `acme-internal-prod`):

1. **Enable Cloud Identity API** — `APIs & Services → Library → "Cloud Identity API" → Enable`.
2. **OAuth consent screen → Data Access → Add or Remove Scopes** — manually add:
   ```
   https://www.googleapis.com/auth/cloud-identity.groups.readonly
   ```
3. **OAuth client → Authorized redirect URIs** — must include `https://<host>/auth/google/callback` for the deployment that uses this client.
4. **OAuth consent screen → Audience** — keep `Internal` (own Workspace tenant only). `External` triggers verification review for the sensitive Cloud Identity scope.

That's it. No service account, no domain-wide delegation, no admin role per user.

## The `security` label trap

Cloud Identity exposes membership listing through `groups/-/memberships:searchTransitiveGroups`. Its `query` (CEL) **must include a label predicate**. Two label types matter:

- `cloudidentity.googleapis.com/groups.discussion_forum` — every Workspace group has it. **Returns 403 "Insufficient permissions"** for non-admin users.
- `cloudidentity.googleapis.com/groups.security` — only security-flagged groups have it as a top-level capability, but in practice **every Keboola Workspace group also carries this label**. **Returns 200** with the full membership list.

Agnes therefore queries with `security` (in `app/auth/providers/google.py`):

```python
"member_key_id == '<email>' && 'cloudidentity.googleapis.com/groups.security' in labels"
```

Switching to `discussion_forum` will silently break for everyone but Workspace admins.

The fetch is **three-state** so soft-fail and "API said zero groups" are
distinguishable:

- `[...]` — the API returned this list.
- `[]`   — the API answered with zero groups for this user.
- `None` — soft fail (missing config, API 4xx/5xx, metadata server unreachable).

The OAuth callback uses the distinction:

- `None` + cached `google_sync` rows → pass-through (returning user during
  a transient outage).
- `None` + no cached rows → deny login (`/login?error=group_check_unavailable`),
  because we can't verify a first-timer's eligibility.
- `[]` or list with no prefix matches under a non-empty
  `AGNES_GOOGLE_GROUP_PREFIX` → deny login
  (`/login?error=not_in_foundryai_group`).
- list with at least one prefix match → sync as usual.

## Storage + use

`app/auth/providers/google.py:google_callback` runs on every Google sign-in:

1. Fetch via `_fetch_google_groups(access_token, email)` → list of `{"id": "<email>", "name": "<displayName>"}`.
2. Write to `request.session["google_groups"]` (Starlette signed-cookie session — per-user, not in DB).
3. Failures (403, 401, network, 4xx) are swallowed and become `[]` so login never breaks.

Display: `app/web/templates/profile.html` reads `session.google_groups` and renders the list. Empty state explains "Groups are populated when you sign in with Google on a Workspace-enabled tenant."

**Not in DB.** Admin views (e.g. `/admin/users`) can't see other users' groups today — adding a `users.groups` column + persisting on callback is the path forward when that's needed.

**Refresh.** A user's stale session keeps stale groups. `Logout → sign in again` is the only refresh.

## Local-dev mock (no Google round-trip)

When developing on `localhost` with `LOCAL_DEV_MODE=1`, Google OAuth never runs, so `session.google_groups` would normally stay empty and group-aware UI/code paths can't be exercised. Set `LOCAL_DEV_GROUPS` to inject a mocked membership list:

```bash
export LOCAL_DEV_GROUPS='[{"id":"engineers@example.com","name":"Engineering"},{"id":"admins@example.com","name":"Admins"}]'
```

The value is a JSON array of objects matching the production shape (`{"id", "name"}`) so the mock and the real callback write the *same* structure into `session.google_groups`. Extra fields are preserved verbatim — handy for forward-compat testing of group attributes Google may return later.

`get_current_user` in `app/auth/dependencies.py` writes the parsed list into the session on every dev-bypass request (compare-then-write — no spurious `Set-Cookie` when the value is unchanged). Malformed input (invalid JSON, non-list, items missing `id`) is logged at WARNING and falls back to `[]` — the dev mock must never break the dev flow.

`docker-compose.local-dev.yml` carries a commented example at the right escape level for Compose YAML. **Never set this in production** — the variable is only honored when `LOCAL_DEV_MODE=1`.

## Debugging

`scripts/debug/probe_google_groups.py` — stdlib, takes a Playground-issued OAuth access token + email, hits 6 candidate endpoints, prints raw response. Use this **before** changing the production query — saves a deploy cycle per attempt.

```bash
python3 scripts/debug/probe_google_groups.py "ya29.…" user@keboola.com
```

Token via [OAuth 2.0 Playground](https://developers.google.com/oauthplayground/) → gear icon → own credentials → request the three scopes (`cloud-identity.groups.readonly`, `cloud-identity.groups`, `admin.directory.group.readonly`) → exchange code → copy access token.

## Group prefix filter and external linking (v15)

Production deployments typically curate a small set of "Agnes-relevant"
Workspace groups under a common naming prefix (e.g. `grp_foundryai_admin@`,
`grp_foundryai_finance@`). The `AGNES_GOOGLE_GROUP_PREFIX` env var teaches
Agnes that convention and turns it into two behaviors:

1. **Filter** — only Workspace groups whose email starts with the prefix
   are mirrored into Agnes's `user_groups` table. Other Workspace groups
   the user belongs to are ignored entirely; they don't pollute the admin
   UI and don't grant any access.
2. **Gate** — Google logins by users with no membership in any prefix-matching
   group are denied at the OAuth callback with
   `?error=not_in_foundryai_group`. Set the prefix and you get a strict
   "must be in an Agnes Workspace group to use Agnes" policy.

Set the var empty (the OSS default) to disable both behaviors and preserve
legacy "mirror everything, no gate" semantics for non-Workspace deployments.

### Group derivation and the `external_id` link

Each prefix-matching Workspace group becomes an Agnes `user_groups` row
with two fields tied to it:

- **`name`** — derived from the group's email by stripping the prefix from
  the local part and capitalizing the first letter. Example: prefix
  `grp_foundryai_`, Workspace group `grp_foundryai_finance@groupon.com` →
  Agnes group `Finance`. Admins can rename the display name later via
  `/admin/groups/<id>` for non-system rows; system rows (`Admin`,
  `Everyone`) are name-locked.
- **`external_id`** — the full Workspace group email. This is the durable
  link: it is set once at creation (or via the promote path below) and
  cannot be edited afterward. The Google sync re-uses the link to find the
  same Agnes row on every login, even after a display-name rename.

The system `Admin` and `Everyone` rows are seeded at startup with
`external_id IS NULL`. The first time a user signs in who is a member of
`<prefix>admin@<domain>` or `<prefix>everyone@<domain>`, the sync's
*promote* path attaches that email to the existing system row instead of
creating a duplicate.

### Admin UI is read-only on bound groups

Once an Agnes group has a non-NULL `external_id`, its membership is
sourced authoritatively from Google. The admin UI on `/admin/groups/<id>`
hides the "Add member" form, banners *"Members are synced from Google
Workspace — read-only here. Edit at admin.google.com"*, and the membership
API endpoints return `409 Conflict { "code": "external_group_readonly" }`
if anyone bypasses the UI to attempt an admin-source mutation. The
google-sync writer (`replace_google_sync_groups`) and the SEED_ADMIN_EMAIL
bootstrap (`source='system_seed'`) bypass the guard intentionally — only
admin-source writes are blocked.

### Auto-Everyone removed

Pre-v15 every new user was implicitly placed in the `Everyone` group via a
`source='system_seed'` row. v15 removes that — Everyone membership now
comes from being in `<prefix>everyone@` (Google logins) or from explicit
admin assignment to `Everyone` while it has not yet been bound (which the
admin UI guard prevents once the binding is in place).

The v15 cleanup migration deletes the stale `system_seed` Everyone rows
the v13 backfill produced (`added_by='system:v13-backfill'`) and removes
orphan email-named google-synced groups that the pre-v15 sync inserted
unconditionally — protected against accidental data loss by skipping any
group already referenced from `resource_grants`.

### Customer-specific configuration

The OSS `agnes-the-ai-analyst-infra` Terraform module exposes a
`google_group_prefix` variable (default `""`). Set it to your prefix in
the consumer infra (private repo) and `terraform apply` — the value lands
in the VM's `.env` as `AGNES_GOOGLE_GROUP_PREFIX`, and the next user login
exercises the filter and gate. App image versions before v15 ignore the
env var, so the infra change can land first or after the app rollout.
