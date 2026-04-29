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

1. Fetch via `fetch_user_groups(email)` (in `app/auth/group_sync.py`) → list of Workspace group emails the user is a member of (transitive).
2. **Filter** by the optional `AGNES_GOOGLE_GROUP_PREFIX` env var. If set (e.g. `grp_foundryai_`), only emails whose local part starts with the prefix survive into Agnes; the rest are discarded. If unset, every fetched group is mirrored (legacy behavior).
3. **System-group mapping**. Two optional env vars route specific Workspace emails into the seeded system rows instead of creating fresh `user_groups` entries:
   - `AGNES_GROUP_ADMIN_EMAIL` — when set, membership in the matching Workspace group adds the user to the seeded `Admin` row.
   - `AGNES_GROUP_EVERYONE_EMAIL` — same, for `Everyone`.
   This lets operators have a Workspace group like `grp_foundryai_admin@example.com` show up in Agnes as the canonical `Admin` system group (with the same `is_system=TRUE` semantics, the same membership-table id) — no parallel "near-Admin" row.
4. **Login gate**. If `AGNES_GOOGLE_GROUP_PREFIX` is set AND the fetch returned a non-empty list AND none of those groups match the prefix → the callback redirects to `/login?error=not_in_foundryai_group`. The prefix gate fires only on a real, prefix-mismatched answer; if Cloud Identity returned an empty list (transient failure or genuine no-membership), the previous cached snapshot is preserved (fail-soft) and the login proceeds — locking returning users out on a flaky API call would be worse than the alternative.
5. Surviving groups land in `user_group_members` with `source='google_sync'`, the underlying `user_groups` row's `name` is the **full Workspace email** (no separate `external_id` column — the email IS the canonical identifier), `created_by='system:google-sync'`. Admin UI strips the prefix and `@domain` for display ("grp_foundryai_finance@example.com" → "Finance" big + email subtitle small).
6. The previous Google-sync set is wholesale replaced (DELETE + INSERT for `source='google_sync'` rows) so a removed Workspace membership disappears immediately. Admin-added memberships (`source='admin'`) are preserved — Google sync only touches its own rows.

**Read-only admin UI on Google-managed rows.** The admin UI hides the Edit / Delete affordances on rows owned by Google sync (`created_by='system:google-sync'`) and on the seeded `Admin` / `Everyone` rows when their email-mapping env var is set. The REST API enforces the same rule: PATCH / DELETE / add-member / remove-member return `409 google_managed_readonly` for these rows. To add or remove members, an operator changes Workspace membership at admin.google.com and the user signs in again to Agnes.

**No more implicit Everyone.** The auto-`system_seed` insert into `Everyone` for every new user was removed when prefix-mapping landed. Every membership now traces to a real source row (`admin`, `google_sync`, or an explicit `system_seed`). If you want plugins visible to "everyone in the company", grant them on a Workspace group every employee belongs to, mapped to `Everyone` via `AGNES_GROUP_EVERYONE_EMAIL`.

The `user_group_members` table is the single source of truth for group memberships, used by:
- RBAC authorization (`app/auth/access.py`) — `require_resource_access` checks group grants
- Admin UI (`/admin/access`) — member lists, grant counts
- CLI (`da admin group members`) — group membership queries
- Marketplace filtering (`src/marketplace_filter.py`) — plugin access based on group grants

**Refresh.** Memberships are refreshed on every Google sign-in. A user's stale memberships persist until their next login.

## Custom (admin-managed) groups

Admins can still create / rename / delete groups manually via `/admin/groups`. Two caveats vs. the prefix-mapped flow:

- A renamed group's primary key (`id`) stays put, but DuckDB's UNIQUE constraint on `name` combined with the FK from `user_group_members.group_id` makes renaming a populated group awkward — the operator must clear members + grants first, rename, then re-add. Documented limitation; the same constraint blocks the prefix-mapping design from using `external_id` so the email is the name.
- System groups (`Admin`, `Everyone`) refuse renames at the repository level regardless of `created_by` — those names are referenced from code (`app.auth.access`, marketplace filter, the email-mapping check) and must not move.

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
