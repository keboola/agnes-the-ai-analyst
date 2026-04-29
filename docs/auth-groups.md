# Google Workspace Groups in Agnes

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

## Storage + use

`app/auth/providers/google.py:google_callback` runs on every Google sign-in:

1. Fetch via `fetch_user_groups(access_token, email)` (in `app/auth/group_sync.py`) → list of `{"id": "<email>", "name": "<displayName>"}`.
2. Write to `user_group_members` table with `source='google_sync'` (DuckDB-backed, persistent across sessions).
3. The previous Google-sync set is wholesale replaced (DELETE + INSERT for `source='google_sync'` rows) so a removed Workspace membership disappears immediately.
4. Admin-added memberships (`source='admin'`) are preserved — Google sync only touches its own rows.
5. **Fail-soft**: If the Cloud Identity API returns an error (403, 401, network), the callback preserves existing memberships instead of wiping them. This prevents a transient API outage from silently dropping all Workspace-synced group memberships.

The `user_group_members` table is the single source of truth for group memberships, used by:
- RBAC authorization (`app/auth/access.py`) — `require_resource_access` checks group grants
- Admin UI (`/admin/access`) — member lists, grant counts
- CLI (`da admin group members`) — group membership queries
- Marketplace filtering (`src/marketplace_filter.py`) — plugin access based on group grants

**Refresh.** Memberships are refreshed on every Google sign-in. A user's stale memberships persist until their next login.

## Local-dev mock (no Google round-trip)

When developing on `localhost` with `LOCAL_DEV_MODE=1`, Google OAuth never runs, so group memberships would normally stay empty. Set `LOCAL_DEV_GROUPS` to inject a mocked membership list:

```bash
export LOCAL_DEV_GROUPS='[{"id":"engineers@example.com","name":"Engineering"},{"id":"admins@example.com","name":"Admins"}]'
```

The value is a JSON array of objects matching the production shape (`{"id", "name"}`). `get_current_user` in `app/auth/dependencies.py` writes the parsed list into `user_group_members` on every dev-bypass request.

`docker-compose.local-dev.yml` carries a commented example at the right escape level for Compose YAML. **Never set this in production** — the variable is only honored when `LOCAL_DEV_MODE=1`.

## Debugging

`scripts/debug/probe_google_groups.py` — stdlib, takes a Playground-issued OAuth access token + email, hits 6 candidate endpoints, prints raw response. Use this **before** changing the production query — saves a deploy cycle per attempt.

```bash
python3 scripts/debug/probe_google_groups.py "ya29.…" user@keboola.com
```

Token via [OAuth 2.0 Playground](https://developers.google.com/oauthplayground/) → gear icon → own credentials → request the three scopes (`cloud-identity.groups.readonly`, `cloud-identity.groups`, `admin.directory.group.readonly`) → exchange code → copy access token.
