# Google Workspace Groups in /profile

How Agnes pulls a user's group memberships at Google sign-in and where they end up.

## Google Cloud setup (per OAuth client / project)

In the GCP project hosting the OAuth client (for Keboola dev: `kids-ai-data-analysis`):

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
