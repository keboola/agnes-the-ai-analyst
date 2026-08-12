# Keboola auth provider + per-instance auth provider allowlist

**Date:** 2026-08-12
**Status:** Approved design, pre-implementation

## Motivation

Instances whose data source is Keboola should let users authenticate with
their existing Keboola platform identity instead of (or in addition to)
Google OAuth, magic link, or password. Two complementary needs:

1. **Web login via Keboola OAuth** — "Sign in with Keboola" on the login
   page, so an instance can run with Keboola as its only identity provider.
2. **API authentication via a Keboola Storage API token** — the standard
   `X-StorageApi-Token` header, verified per-request against the Keboola
   stack, as an alternative to an Agnes PAT for scripts and integrations.

Separately, operators need **explicit control over which login methods an
instance offers**. Today availability is implicit (each provider's
`is_available()`: Google shows iff OAuth credentials are configured,
password/email always). Different customers need different sets: only
Google, only password, only Keboola.

## Scope — three pieces

### 1. Keboola OAuth web-login provider

New `app/auth/providers/keboola.py`, modeled on `google.py`:

- `is_available()` — true iff `auth.keboola` config is complete
  (`oauth_host`, `client_id`, `client_secret`, `project_id`, and a
  resolvable stack URL). The allowlist (piece 3) is a separate layer on
  top — `is_available()` reports config-completeness only.
- **Login route** redirects to `{oauth_host}/oauth/authorize` with a
  `state` CSRF token (same mechanism as the Google provider).
- **Callback route** exchanges the code at `{oauth_host}/oauth/token`,
  then verifies the access token via
  `GET {stack_url}/v2/storage/tokens/verify` with
  `Authorization: Bearer <access_token>`.
- From the verify response it reads: token `id`, `owner.id` +
  `owner.name` (project), `adminOwner.id/email/name` (the human user).
- **Project binding:** `owner.id` must equal the configured
  `auth.keboola.project_id`, else the login is rejected. Membership in
  the configured project *is* the trust boundary — the `allowed_domain`
  filter is **not** applied to this provider.
- **Auto-provisioning:** first successful login creates the user from
  `adminOwner.email` through the same path the Google provider uses
  (user row + `ensure_everyone_membership`), then issues a normal Agnes
  session (same JWT/cookie as every other provider). No new user-model
  fields; repositories are reached through the factory, so DuckDB/PG
  parity holds by construction.

### 2. `X-StorageApi-Token` header auth on the API

A new branch in `get_current_user` (`app/auth/dependencies.py`), ahead of
the bearer-token path:

- Fires only when the request carries an `X-StorageApi-Token` header
  **and** `auth.keboola.allow_token_header` is true (and the provider is
  enabled). Otherwise the header is ignored entirely.
- The plain Storage API token is verified via
  `GET {stack_url}/v2/storage/tokens/verify` with the token in the same
  `X-StorageApi-Token` header.
- **Project binding** as above: `owner.id` must match the configured
  project, else 401.
- **Admin tokens only:** a token without `adminOwner` (bucket-scoped /
  non-admin tokens) is rejected with an explicit 401 detail — not a
  generic failure.
- **Existing users only:** `adminOwner.email` is mapped to an existing
  `users` row. Unknown user → 401 with a detail telling them to sign in
  via the web login first. The header path never provisions accounts —
  it is an alternative credential, not an onboarding channel.
- After mapping, the request proceeds with the user's normal identity and
  RBAC (equivalent to a PAT-authenticated request).
- **Verify cache:** successful verifications are cached ~60 s, keyed by
  the SHA-256 of the token, so bursts of API calls don't hammer the
  Keboola stack. Revocation upstream therefore takes effect within a
  minute. Failed verifications are never cached; the rate limit in the
  Security section bounds abuse of the failure path.

### 3. Per-instance auth provider allowlist

New optional `auth.providers` list in `instance.yaml`:

```yaml
auth:
  providers: [keboola]          # any of: google, email, password, keboola
```

- **Unset ⇒ today's implicit behavior**, unchanged for every existing
  instance (all providers whose `is_available()` is true are offered).
- When set, the effective offer is `allowlist ∩ is_available()`.
- A provider excluded by the allowlist is not merely hidden on the login
  page — its login/callback endpoints return 404.
- The login page keeps deriving its buttons from provider availability;
  no template fork.

## Configuration

```yaml
auth:
  providers: [keboola]              # optional; unset = current behavior
  keboola:
    oauth_host: "https://<oauth-host>"      # Keboola OAuth server for the stack
    client_id: "${KEBOOLA_OAUTH_CLIENT_ID}"
    client_secret: "${KEBOOLA_OAUTH_CLIENT_SECRET}"
    stack_url: "https://<connection-host>"  # optional when the instance's
                                            # data source is Keboola — defaults
                                            # to the data-source stack URL
    project_id: <numeric project id>        # binding; other projects → 401
    allow_token_header: true                # enable piece 2 independently
```

Secrets stay in `.env` (never in `instance.yaml` literals).

## Decisions taken

- Project binding replaces `allowed_domain` for this provider; the
  configured Keboola project is the authority on who belongs to the
  instance.
- No project-side feature flag is required (the platform-side feature
  gating some Keboola chat products use is their product concern; our
  gate is `project_id`).
- No DB schema change, no migration. No new repository methods expected;
  if any are added, the `_pg.py` sibling + contract test land in the same
  PR per the dual-backend rules.
- CLI login with a Storage API token is out of scope (the header already
  works for scripts; the CLI keeps its PAT flow). Candidate follow-up.

## Security

Per `.claude/skills/agnes-conventions/references/security.md`:

- Tokens never appear in logs, audit rows, URLs, or argv — only SHA-256
  hashes where a correlation key is needed.
- Verification egress goes only to the operator-configured `oauth_host` /
  `stack_url` — never to a host derived from request input.
- OAuth flow carries a `state` CSRF token; callback is a GET that only
  completes the flow (session issuance), mutations stay POST+CSRF.
- Rate limiting on failed verifications (same limiter the other auth
  endpoints use) so the header path can't be used to brute-force or to
  amplify traffic against the stack.
- Plaintext tokens are never persisted; the verify cache stores only the
  hash key and the verified identity claims.

## Testing

- Provider unit tests with a mocked Keboola API: verify OK; wrong
  project; non-admin token; unknown user (header path); OAuth callback
  happy path + `state` mismatch.
- Allowlist gating: login page renders only allowed providers; excluded
  providers' endpoints 404; unset `auth.providers` preserves today's
  behavior byte-for-byte (guard test).
- Header auth end-to-end through `TestClient`: header → verified → RBAC
  applies; cache TTL honored; `allow_token_header: false` ignores the
  header.

## Out of scope

- CLI `agnes` login via Storage API token.
- Multi-project instances (one instance binds to exactly one project).
- Admin UI for editing `auth.providers` (instance.yaml is the operator
  surface).
