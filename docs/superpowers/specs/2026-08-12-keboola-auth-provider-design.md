# Keboola auth provider + per-instance auth provider allowlist

**Date:** 2026-08-12
**Status:** Approved design, revised after three independent reviews
(security, architecture-fit, platform-facts), pre-implementation

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

## External dependency (resolve before promising to a customer)

The Keboola OAuth authorization server (`/oauth/authorize`,
`/oauth/token` on the connection host) exists on every stack but is
**publicly undocumented**, and OAuth client registration
(`client_id`/`client_secret`) is a Keboola-internal provisioning step,
not operator self-service. Before this feature is committed to any
deployment, confirm with Keboola that a client registration will be
issued for the target stack (including single-tenant stacks). The
token-header path (piece 2) has no such dependency.

## Scope — three pieces

### 1. Keboola OAuth web-login provider

New `app/auth/providers/keboola.py`, modeled on `google.py`:

- `is_available()` — true iff `auth.keboola` config is complete
  (`client_id`, `client_secret`, `project_id`, and a resolvable stack
  URL). The allowlist (piece 3) is a separate layer on top —
  `is_available()` reports config-completeness only.
- **Login route** redirects to `{oauth_host}/oauth/authorize` with a
  `state` CSRF token (same authlib/session mechanism as the Google
  provider). Post-login redirect targets go through `safe_next_path`,
  as in the Google callback.
- **Callback route** exchanges the code at `{oauth_host}/oauth/token`,
  then verifies the access token via
  `GET {stack_url}/v2/storage/tokens/verify` with
  `Authorization: Bearer <access_token>`.
  *Named assumption:* Bearer acceptance on that endpoint and the
  `adminOwner` field in the verify response are real but publicly
  undocumented platform behavior (the public verify docs show `admin`
  without an email; `adminOwner` is documented only on token detail).
  Both must be handled defensively: missing `adminOwner`/email →
  explicit login failure, never a crash.
- From the verify response it reads: token `id`, `owner.id` +
  `owner.name` (project), `adminOwner.id/email/name` (the human user).
- **Project binding:** the verified project must match the configured
  `auth.keboola.project_id`. The comparison reuses the existing
  `project_identity` semantics from the admin source-connection code
  (string coercion on both sides, explicit reject when `owner.id` is
  absent) — never a naive `==` (int vs `${ENV}`-string, None holes).
  Membership in the configured project *is* the trust boundary — the
  `allowed_domain` filter is **not** applied to this provider.
  Because a multi-project user picks the project on Keboola's own
  authorize screen (no parameter pins it), a mismatch must render a
  friendly page naming the expected project ("retry and pick project
  X"), not a bare 401.
- **Role gate:** optional `auth.keboola.allowed_roles` (values of the
  verify response's `admin.role`, e.g. `[admin, share]`). Unset = any
  role. Operators must understand the default: **anyone the Keboola
  project admits — including `guest`, `readOnly`, and external
  collaborator accounts — can create an Agnes account** and receives
  Everyone-group access. This is stated in the config docs, not only
  here.
- **Auto-provisioning:** first successful login creates the user through
  a **shared provisioning helper extracted from the Google callback** —
  user row + `ensure_everyone_membership` + the v39 system-plugin
  fanout (`fanout_system_for_user`) + the deactivated-account rejection.
  Extracting the helper (and pointing the Google provider at it) is in
  scope; inline duplication of the Google code is not acceptable — that
  is the known dual-surface drift trap.
- Then issues a normal Agnes session (`create_access_token` +
  `access_token` cookie), identical to the other providers.
- **Identity mapping caveat (accepted v1 risk):** users are keyed by
  email (`adminOwner.email`). If a user's Keboola email changes, their
  next login provisions a fresh Agnes account; history stays with the
  old one. Storing `adminOwner.id` would need a schema change and is
  deferred until real demand.

### 2. `X-StorageApi-Token` header auth on the API

A new branch in `get_current_user` (`app/auth/dependencies.py`):

- Fires only when the request carries an `X-StorageApi-Token` header
  **and** the `keboola_token_header` switch is on (see Configuration)
  **and** the header path's own config is complete: `stack_url` +
  `project_id`. It deliberately does NOT require the OAuth
  `client_id`/`client_secret` — an instance can run the token header
  without any OAuth client (see External dependency) — and it is
  independent of the `auth.providers` allowlist, which governs login
  offerings; the switch (default off) is this path's explicit gate.
  Otherwise the header is ignored entirely.
- **Precedence:** consulted only when no `Authorization` bearer
  credential and no session cookie are present — a Storage token never
  shadows an established Agnes credential.
- The plain Storage API token is verified via
  `GET {stack_url}/v2/storage/tokens/verify` with the token in the same
  `X-StorageApi-Token` header, with a short bounded timeout.
- **Project binding** exactly as in piece 1 (same helper).
- **Master tokens only:** the gate is `isMasterToken: true` (equivalently
  the presence of the `admin` block). `adminOwner` alone is NOT a
  discriminator — the platform back-fills it through the token's
  creator chain, so a restricted bucket-scoped token created by an admin
  verifies *with* an `adminOwner`; accepting those would let any holder
  of a scoped service token authenticate as the human who created it.
  The optional `allowed_roles` gate from piece 1 applies here too.
- **Existing users only:** `adminOwner.email` is mapped to an existing
  `users` row. Unknown user → 401 with a detail telling them to sign in
  via the web login first. The header path never provisions accounts —
  it is an alternative credential, not an onboarding channel.
- **Credential classification — the header is a non-interactive,
  PAT-like credential.** Concretely:
  - `require_session_token` (and any other place that sniffs the
    credential kind from `Authorization`/cookie) must recognize and
    reject it, exactly as it rejects PATs — otherwise a revocable
    Storage token could mint a persistent Agnes PAT, connect MCP, or
    manage agents. This is the single most security-critical line item
    in this spec.
  - The resolved identity carries `credential_surface='stack'` (the
    same narrowing PATs get), never the implicit `'all'`.
  - The elevation middleware treats it like bearer auth (as it treats
    PATs), not like a browser session — decided, not accidental.
- After classification, the request proceeds with the user's normal
  RBAC. The `users_repo` lookup, `active` check, and group resolution
  run per request, **uncached** — only the upstream verify result is
  cached.
- **Verify cache:** successful verifications are cached ~60 s, keyed by
  the SHA-256 of the token, so bursts of API calls don't hammer the
  Keboola stack. Revocation upstream therefore takes effect within a
  minute. Failed verifications are never cached.
- **Flood guard (replaces the unusable route-level limiter):** the
  slowapi decorator can't wrap a dependency, so the branch carries its
  own guard — a per-IP (via `trusted_client_ip`) *and* a global cap on
  cache-miss verify calls per window, plus backoff after repeated
  failures from one IP. Distinct-invalid-token floods must neither
  amplify traffic against the customer's stack nor exhaust the
  threadpool (each verify is a blocking outbound call).
- **Where the header is honored:** only routes authenticated through
  `get_current_user`. Surfaces with their own auth (MCP HTTP middleware,
  marketplace git Basic-auth, notifications WebSocket handshake, MCP
  OAuth consent) do NOT accept it — documented, not implied. "Equivalent
  to a PAT" refers to RBAC authority, not surface coverage.

### 3. Per-instance auth provider allowlist

New optional `auth.providers` list in `instance.yaml`:

```yaml
auth:
  providers: [keboola]          # any of: google, email, password, keboola
```

- **Unset ⇒ today's implicit behavior**, unchanged for every existing
  instance (all providers whose `is_available()` is true are offered).
- **Explicitly empty list ⇒ rejected**: config validation error at
  startup (logged, treated as unset so the instance stays reachable) and
  a 400 from the admin server-config API — an admin must not be able to
  lock every user out with one overlay write.
- When set, the effective offer is `allowlist ∩ is_available()`,
  computed by **one shared helper** consumed by every
  provider-enumerating surface. The review found three today:
  1. the `/login` page derivation (`app/web/router.py` — note the
     password button is currently appended unconditionally, so the
     filter must be inserted, not just intersected),
  2. the `/login/password` and `/login/email` sub-pages (each renders a
     Google button off raw `is_available()`),
  3. the MCP OAuth consent flow's `_login_url`
     (`app/auth/mcp_oauth.py`), which must fall back to an allowed
     provider instead of hard-coding Google.
- A provider excluded by the allowlist is not merely hidden — its
  endpoints return 404 via a router-level dependency. The per-provider
  endpoint sets are:
  - `google`: `/auth/google/*`
  - `email`: `/auth/email/*` (magic-link request + callback), the
    `/login/email` page
  - `password`: `/auth/password/*` (login, reset, setup), the
    `/login/password` page, **and the shared-router `POST /auth/token`**
    (password grant — lives in `app/auth/router.py`, easy to miss)
  - `keboola`: `/auth/keboola/*`; the token header path additionally
    requires its switch (piece 2)
- The allowlist **gates issuance only**. Already-issued session JWTs
  (30-day TTL, signature+exp trust, no per-request DB check) remain
  valid after a provider is disabled; the kill switch for a person is
  `users.active`, and removing a user from the Keboola project does not
  end their existing Agnes session either. Stated so operators aren't
  falsely reassured.
- The documented-but-dead `auth.disabled_providers` key in
  `config/instance.yaml.example` is removed in the same PR (nothing
  consumes it; two overlapping vocabularies must not ship).

## Configuration

```yaml
auth:
  providers: [keboola]              # optional; unset = current behavior
  keboola:
    client_id: "${KEBOOLA_OAUTH_CLIENT_ID}"
    client_secret: "${KEBOOLA_OAUTH_CLIENT_SECRET}"
    stack_url: "https://<connection-host>"  # optional when the instance's
                                            # data source is Keboola — defaults
                                            # to the data-source stack URL
    # oauth_host: defaults to stack_url (the OAuth server lives on the
    # connection host); override only if a stack separates them
    project_id: <project id>                # binding; other projects → 401
    # allowed_roles: [admin, share]         # optional; unset = any role
```

- The token-header toggle is **not** a raw config key: per the
  CONTRIBUTING.md switch-registry rule it is an entry in
  `app.switches.SWITCHES` (working name `keboola_token_header`, config
  path `("auth", "keboola", "allow_token_header")`) plus a row in
  `docs/feature-flags.md`. **Default: off** — a plain Storage token
  authenticates with no interactive factor, so enabling it bypasses any
  MFA/SSO the customer enforces on web logins; the feature-flag row and
  config example carry that warning explicitly.
- Secrets stay in `.env` (never in `instance.yaml` literals).
- The `auth` section is editable through `POST /api/admin/server-config`
  (it sits in `_STATIC_EDITABLE_SECTIONS`), so "instance.yaml is the
  operator surface" is not the whole truth: `auth.keboola.stack_url`
  and `auth.keboola.oauth_host` must be added to the admin API's
  `_URL_BEARING_FIELDS` so overlay writes get the same URL validation
  as `data_source.keboola.stack_url`.

## Decisions taken

- Project binding replaces `allowed_domain` for this provider; the
  configured Keboola project is the authority on who belongs to the
  instance. Consequence (stated for operators): guests, read-only
  members, and external collaborators of that project self-provision as
  ordinary Agnes users unless `allowed_roles` narrows it.
- Master-token gate (`isMasterToken`), not `adminOwner` presence —
  see piece 2 for why the latter is unsafe.
- The Keboola OAuth access/refresh tokens are **discarded after login**
  (deliberate non-goal: Agnes's data plane uses instance-level Keboola
  credentials; per-user Keboola API calls would need re-consent later).
- No project-side feature flag is required; our gate is `project_id`
  (+ optional `allowed_roles`).
- No DB schema change, no migration. No new repository methods expected;
  if any are added, the `_pg.py` sibling + contract test land in the same
  PR per the dual-backend rules.
- Email-keyed identity with the documented account-fork caveat on email
  change (piece 1); `adminOwner.id` storage deferred.
- CLI login with a Storage API token is out of scope (the header already
  works for scripts; the CLI keeps its PAT flow). Candidate follow-up.

## Security

Per `.claude/skills/agnes-conventions/references/security.md`:

- Tokens never appear in logs, audit rows, URLs, or argv — only SHA-256
  hashes where a correlation key is needed.
- Verification egress goes only to the operator-configured `oauth_host` /
  `stack_url`, and **both are re-validated with
  `_validate_url_not_private` at every outbound call** (use-time, not
  store-time — the same DNS-rebind / metadata-endpoint guard the admin
  source-connection code applies). httpx TLS verification stays at its
  default (on); a client controlling the token must not be able to MITM
  the verify.
- OAuth flow carries a `state` CSRF token; callback is a GET that only
  completes the flow (session issuance), mutations stay POST+CSRF;
  post-login redirects go through `safe_next_path`.
- Flood control for the header path is the in-dependency guard described
  in piece 2 (per-IP + global caps, bounded timeout, failure backoff) —
  keyed on `trusted_client_ip`, never leftmost XFF.
- Plaintext tokens are never persisted; the verify cache stores only the
  hash key and the verified upstream claims. Agnes-side user state
  (active flag, groups) is never cached.
- The header credential is classified non-interactive everywhere a
  credential kind is derived (`require_session_token`, elevation,
  `credential_surface`) — see piece 2.

## Testing

- Provider unit tests with a mocked Keboola API: verify OK; wrong
  project; missing `owner.id`; non-master token (with back-filled
  `adminOwner` — the escalation case); missing `adminOwner`; role gate;
  unknown user (header path); OAuth callback happy path + `state`
  mismatch + project-mismatch page.
- Header credential classification: `POST /auth/tokens` (PAT mint), MCP
  connect, and agent-management endpoints all reject
  `X-StorageApi-Token`-authenticated requests; `credential_surface` is
  `'stack'`; header ignored when a bearer credential is present.
- Allowlist gating across **all three** enumeration surfaces (login
  page, `/login/*` sub-pages, MCP OAuth `_login_url`); excluded
  providers' endpoints 404 **including `POST /auth/token`** for
  password; explicit `[]` rejected; unset `auth.providers` preserves
  today's behavior byte-for-byte (guard test).
- Shared provisioning helper: Google and Keboola logins both run
  create + Everyone + v39 fanout + deactivated rejection (one test per
  provider against the helper's contract).
- Header auth end-to-end through `TestClient`: header → verified → RBAC
  applies; cache TTL honored; flood guard trips on distinct-invalid-token
  bursts; switch off ⇒ header ignored.
- Switch registry: `keboola_token_header` present in
  `app.switches.SWITCHES` + `docs/feature-flags.md`
  (tests/test_switches.py, tests/test_admin_configure_api.py).
- OpenAPI snapshot refresh (`make update-openapi-snapshot`) — the new
  `/auth/keboola/*` routes change the schema.

## Out of scope

- CLI `agnes` login via Storage API token.
- Multi-project instances (one instance binds to exactly one project).
- Dedicated admin UI for editing `auth.providers` (the generic
  server-config editor already reaches it; see Configuration for the
  validation that implies).
- Storing `adminOwner.id` / a provider-identity table (revisit if email
  churn becomes real).
- Per-user Keboola API calls with stored OAuth tokens.
- Self-service project onboarding from the login (register the
  authenticated project as a source, pick tables, import the semantic
  layer, data apps) — tracked in #1286.
