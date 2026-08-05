# MCP OAuth sources — outbound OAuth client for upstream MCP servers

**Status:** v2 — review findings folded in (architecture + security passes)
**Date:** 2026-07-30
**Owner:** platform

## Problem

Agnes consumes upstream MCP servers ("MCP sources") with static credentials
only: `auth_method ∈ {bearer, basic, none}` plus a vault- or per-user-stored
secret (`connectors/mcp/client.py::_build_http_headers`). Modern hosted MCP
servers are increasingly **OAuth-protected resources** (MCP auth spec: RFC 9728
protected-resource metadata + RFC 8414 AS metadata + RFC 7591 dynamic client
registration + authorization-code flow with PKCE). Today those servers cannot
be registered as Agnes sources at all — operators fall back to self-hosting a
stdio twin of the server, or to undocumented static-token workarounds, and
every analyst hand-copies tokens into `/me/connections`.

Concrete pain (observed live): a hosted per-region MCP endpoint responds
`401` with `WWW-Authenticate: Bearer … resource_metadata="…/.well-known/
oauth-protected-resource"` and its authorization server expects dynamic client
registration — nothing a static bearer header can satisfy. Static personal
tokens that DO work elsewhere expire or violate org token policies, and the
copy-paste flow is the single largest onboarding friction for per-user
sources.

## Goal

`auth_method='oauth'` on `mcp_sources`: an analyst clicks **Connect** on
`/me/connections` (or the admin's inline "Your connection" panel), authorizes
in their browser, and Agnes transparently uses + refreshes their tokens for
every introspect/test/forward on that source. No secret ever passes through a
human's clipboard.

Non-goals (this iteration):

- OAuth for **stdio** sources (subprocess servers keep env-token auth).
- Client-credentials / machine-to-machine grants (only authorization-code +
  PKCE). **OAuth sources are always `scope='per_user'`** — see §1; a
  shared-scope OAuth source is a validation error.
- Acting as a token *issuer* — Agnes already has that on the inbound side
  (`app/auth/mcp_oauth.py`); this spec is the outbound mirror image and reuses
  none of its issuance state, only its style conventions.

## Building blocks — reuse vs. modify

| Block | Where | Disposition |
|---|---|---|
| Inbound OAuth server (issuer) | `app/auth/mcp_oauth.py`, discovery routes in `app/api/mcp_streamable.py` | conventions only (route naming, tests style) |
| Per-user secret vault (Fernet, write-only) | `app/secrets_vault.py::PerUserSecretsRepository` (`mcp_user_secrets`) | untouched; OAuth tokens live in a NEW sibling table (below) |
| Per-user connect UX | `/me/connections` + admin inline panel; `/api/mcp/sources/{id}/my-secret*` | extended (Connect/Disconnect for OAuth sources) |
| Caller-identity threading | `caller_user_id` through `connectors/mcp/client.py` | reused as-is |
| Per-user fail-closed gate | `app/api/mcp_policy.py::enforce_per_user_credential` | **MODIFIED** — must learn the OAuth token table (today it consults only `per_user_secrets_repo()`); an expired-and-unrefreshable OAuth row counts as missing |
| Admin probe identity | `app/api/admin_mcp.py::_probe_caller_user_id` | **MODIFIED** — must also check `mcp_user_oauth_tokens` for the calling admin, else probes silently fall back to caller-less |
| Grant gate on per-user endpoints | `app/api/mcp_user_secrets.py::_require_source_grant` | reused on the new authorize/disconnect endpoints + re-checked at callback |
| SSRF-safe outbound fetch | `src/marketplace_asset_mirror.py::_resolve_safe` + `_SSRFGuardTransport` (DNS resolve, private/loopback/link-local/metadata IP rejection, IP-pinned connection, re-validation on EVERY redirect hop) | **extracted into a shared helper** and used for ALL discovery/DCR/token traffic. `is_attach_host_allowed` is NOT used here — it is a fail-open hostname allowlist bound to the ATTACH egress knob, wrong primitive and wrong operator surface |
| Coordination locks | `app/coordination/base.py::lease_acquire`/`lease_release` (raw primitives; NOT `run_with_lease`, which is a long-running-singleton loop) | refresh single-flight |
| HTTP client | `authlib` (already a dependency), `httpx` | AS metadata/DCR/token endpoints |

## Design

### 1. Data model (schema v108 → v109, both ladders in the same PR)

New table `mcp_source_oauth_clients` — one row per OAuth source (Agnes's own
client registration at the upstream AS). Named to avoid collision with the
inbound issuer's existing `oauth_clients` table (`src/db.py` — Agnes-as-issuer
DCR rows), which is the mirror-image concept:

```
source_id TEXT PK REFERENCES mcp_sources(id)
issuer TEXT NOT NULL              -- AS issuer URL from RFC 8414 metadata
client_id TEXT NOT NULL
client_secret_enc BLOB NULL       -- Fernet; NULL for public clients (PKCE-only)
registration_access_token_enc BLOB NULL
authorization_endpoint TEXT NOT NULL
token_endpoint TEXT NOT NULL
scopes TEXT NULL                  -- space-joined; admin override wins (default empty)
created_at / updated_at TIMESTAMP
```

New table `mcp_user_oauth_tokens` — per `(source_id, user_id)`:

```
source_id TEXT NOT NULL
user_id TEXT NOT NULL
access_token_enc BLOB NOT NULL    -- Fernet, same vault key as mcp_user_secrets
refresh_token_enc BLOB NULL
expires_at TIMESTAMP NULL         -- NULL = non-expiring / unknown
scopes TEXT NULL
created_at / updated_at TIMESTAMP
PRIMARY KEY (source_id, user_id)
```

New table `mcp_oauth_flows` — in-flight authorize flows (PKCE verifier +
state nonce), DB-backed so multi-replica deployments need no sticky sessions
and single-replica DuckDB works identically:

```
nonce TEXT PK
source_id TEXT NOT NULL
user_id TEXT NOT NULL
pkce_verifier_enc BLOB NOT NULL
created_at TIMESTAMP NOT NULL     -- rows expire after 10 min; swept opportunistically
```

Rules:

- Kept **separate from `mcp_user_secrets`**: different lifecycle (refresh
  mutates rows server-side; user secrets are write-only from the user),
  different deletion semantics (revoke-at-AS best-effort on disconnect), and
  the existing table's contract stays untouched.
- **`auth_method='oauth'` forces `scope='per_user'`** — validated in
  `MCPSourceRepository.upsert()` (both backends): oauth+shared and
  oauth+stdio are errors. All existing per-user enforcement keys off `scope`,
  so this coupling is what makes the fail-closed paths fire at all.
- Repos `mcp_source_oauth_clients(_pg).py`, `mcp_user_oauth_tokens(_pg).py`,
  `mcp_oauth_flows(_pg).py` behind the factory; cross-engine contract tests;
  `_v108_to_v109` in `src/db.py` + matching Alembic revision; update any doc
  that states the current schema version.

### 2. Source registration & discovery (admin)

- New admin action `POST /api/admin/mcp-sources/{id}/oauth/register`
  (`require_admin`):
  1. Fetch `{source.url}/.well-known/oauth-protected-resource` (fallback:
     probe the 401 `WWW-Authenticate: resource_metadata=`). **This first hop
     already goes through the SSRF-safe transport** — the `resource_metadata`
     pointer comes from a live upstream response and is attacker-influenceable
     if the upstream is later compromised.
  2. RFC 8414 metadata fetch → endpoints. All URLs (issuer, authorization,
     token, registration endpoints) must be **https** and pass the SSRF-safe
     resolver; redirects on ANY discovery/DCR/token call are re-validated
     hop-by-hop by the shared transport (no check-then-fetch gap).
  3. **PKCE fail-closed:** if the AS's `code_challenge_methods_supported`
     does not include `S256`, registration fails with an explanatory error —
     never downgrade to `plain` or no-PKCE (RFC 9700 §4.1).
  4. RFC 7591 dynamic registration with
     `redirect_uris=[{server_url}/api/mcp/oauth-client/callback]`,
     `grant_types=["authorization_code","refresh_token"]`,
     `token_endpoint_auth_method` per AS support (prefer
     `client_secret_basic`, accept `none` for PKCE-public).
  5. Persist `mcp_source_oauth_clients` row. Idempotent: re-register replaces
     the row (old registration revoked best-effort via the registration
     access token).
- Manual client config escape hatch: `PUT …/oauth/client` (`require_admin`)
  accepting `{client_id, client_secret?, authorization_endpoint,
  token_endpoint}` for AS's without DCR — same https + SSRF validation.
- Admin UI: source detail shows discovery status + "Register OAuth client"
  in place of the vault-secret card when `auth_method='oauth'`.
- CLI coverage (ratchet): `agnes admin mcp-source oauth-register <id>` and
  `agnes admin mcp-source oauth-client <id> --client-id … --token-endpoint …`.

### 3. Per-user connect flow

Route prefix note: the outbound-client callback lives under
**`/api/mcp/oauth-client/`** — deliberately distinct from the inbound
issuer's `/api/mcp/oauth/*` (consent, token) so the OpenAPI surface keeps the
two flows visually separate.

- `GET /api/mcp/sources/{id}/oauth/authorize` (authenticated; `deny_principal`
  — connect is human-only; **`_require_source_grant`** — an ungranted user
  must not be able to mint and park a token for a source they cannot use;
  rate-limited like `…/my-secret/test`, explicit per-minute cap): creates an
  `mcp_oauth_flows` row (nonce, encrypted PKCE S256 verifier), builds the AS
  authorize URL with `state` = signed (itsdangerous, server secret) blob
  `{source_id, user_id, nonce, exp≤10min}`. Responds `302`.
- `GET /api/mcp/oauth-client/callback?code&state`: requires the normal
  authenticated web session; verifies state signature + expiry + nonce
  single-use (row deleted on first use) + `state.user_id == session user`
  (login-CSRF: no planting tokens into someone else's session) +
  **re-runs `_require_source_grant`** (grants may have been revoked while the
  flow was in-flight); exchanges the code at the token endpoint **resolved
  exclusively from `mcp_source_oauth_clients[state.source_id]`** (never from
  request data — this is the mix-up defense, see §6) with the stored PKCE
  verifier; persists tokens; redirects to `/me/connections?connected={id}`.
- Disconnect: `DELETE /api/mcp/sources/{id}/oauth/connection`
  (`deny_principal` + `_require_source_grant`): best-effort RFC 7009
  revocation, then row delete. CLI: `agnes mcp disconnect <source>`.
- Status: extend `GET …/my-secret` response with `auth_kind:
  'secret'|'oauth'` and `expires_at` so both UIs render one model.
- UI (`me_connections.html` + admin inline panel): for OAuth sources the
  paste-field is replaced by **Connect with {source}** / status chip /
  **Disconnect**. JS work only, same cards.
- CLI connect: `agnes mcp connect <source>` prints the authorize URL and
  polls the status endpoint until connected (device-style UX).

### 4. Token use & refresh (the call seam)

`connectors/mcp/client.py::_build_http_headers` grows an oauth branch:

- Lookup for `auth_method='oauth'`: `mcp_user_oauth_tokens[(source, caller)]`
  only — same fail-closed rule as per_user secrets (no shared fallback; the
  caller-less scheduled path gets no token).
- If `expires_at` within a 60s skew → refresh first. **Single-flight per
  (source, user)** via `lease_acquire`/`lease_release` on the coordination
  backend (raw primitives, short TTL; in-process asyncio lock as the
  single-replica fast path). On Postgres role-split deployments this lock is
  load-bearing correctness (two processes CAN race a refresh and orphan a
  rotated refresh token), not an optimization — PR 1 ships a dedicated
  two-process refresh-race test, not just contract tests.
- Rotated refresh tokens are persisted atomically with the new access token.
- Refresh failure with `invalid_grant` → delete the row (forces re-connect).
  Other AS errors: flattened via `exc_summary` before surfacing; repeated
  failures back off (no hot refresh loop against a broken AS).
- `enforce_per_user_credential` (modified, §Building blocks) treats a
  present-but-expired-and-unrefreshable row as missing, raising the SAME
  `PerUserCredentialMissing` with the same remedy string as secret-backed
  sources — one 403 message contract across both kinds.
- DuckDB single-writer note: token writes ride the same `_system_db_lock`
  path as `mcp_user_secrets` writes; refresh frequency is bounded by token
  lifetime per (source,user), and the single-flight lock caps concurrent
  writers at one per pair.

### 5. Probes & parity surfaces

- Admin introspect/classify/test: `_probe_caller_user_id` **extended** to
  consult `mcp_user_oauth_tokens` too, so probes run under the calling
  admin's own OAuth connection when present (and stay caller-less otherwise).
- `…/my-secret/test` resolves through the new lookup (no new endpoint).
- REST × CLI × MCP coverage: every new endpoint above has a named CLI verb
  (§2/§3). Only `…/oauth/authorize` + `…/oauth-client/callback` use the
  ratchet's OAuth-callback exemption; nothing else is exempted.

### 6. Security checklist (blocking items for the PRs)

- **SSRF:** all outbound OAuth traffic (both discovery hops, DCR, token
  exchange, revocation) through the shared `_SSRFGuardTransport`-style client:
  DNS resolution + private/metadata IP rejection + IP-pinned connections +
  per-hop redirect re-validation; https-only.
- **state:** signed, expiring, single-use (DB row deleted on redemption),
  bound to the session user at callback.
- **PKCE:** S256 always; fail-closed when the AS doesn't advertise S256;
  verifier stored encrypted server-side only.
- **Mix-up defense (RFC 9700 §4.4):** the token endpoint and client identity
  used at redemption come ONLY from `mcp_source_oauth_clients[state.source_id]`
  — never from callback parameters or AS responses. Must-not-regress rule.
  When the AS advertises RFC 9207, validate the `iss` callback parameter as
  defense-in-depth.
- **Grant gating:** `_require_source_grant` on authorize AND re-checked at
  callback; connect/disconnect are `deny_principal` (human-only).
- **Logs:** a dedicated logging filter redacts the query string for
  `/api/mcp/oauth-client/callback` (no such control exists today —
  `app/logging_config.py` only tunes the access-log level, so this is new
  code, not an assumption); operator docs instruct the TLS-terminating proxy
  to drop this path's query string from its access log as well. Tokens and
  `code` never appear in application log payloads.
- **At rest:** tokens + verifiers Fernet-encrypted with the existing vault
  key (409 `vault_key_not_configured` when absent, same as secrets).
- **Rate limits:** explicit per-minute caps on `authorize` (discovery/DCR
  round-trips) and on refresh attempts per (source,user).
- **Audit:** `mcp_oauth.client_register`, `.connect`, `.disconnect`,
  `.refresh_failed` — no token material in payloads.

### 7. Phasing (build plan)

- **PR 1 — foundation (no UI):**
  - schema v109 (both ladders) + three repos (+ `_pg` siblings + factory
    entries + cross-engine contract tests + backend-split guard happy)
  - shared SSRF-safe HTTP helper extracted from `marketplace_asset_mirror`
  - `connectors/mcp/oauth_client.py` (discovery, DCR, token exchange,
    refresh) with the PKCE/mix-up rules above
  - `client.py` oauth branch + single-flight refresh + two-process race test
  - `enforce_per_user_credential` + `_probe_caller_user_id` extensions
  - `MCPSourceRepository.upsert` oauth/per_user coupling validation
  - admin endpoints (`oauth/register`, `oauth/client`) + CLI verbs
  - Feature-complete via REST (connect still possible by manually inserting a
    token row in tests).
- **PR 2 — connect UX:** authorize + callback + disconnect endpoints
  (state/PKCE/grant re-check), log-redaction filter, `/me/connections` +
  admin panel UI, `agnes mcp connect`/`disconnect`, operator docs (proxy log
  note, allowlist posture), status-endpoint `auth_kind`.
- **PR 3 (optional) — hardening:** RFC 7009 revocation, registration
  rotation, field-discovered AS quirks.

Sync-map rows touched: repo parity (three new repos), migration ladders +
schema-version docs, REST×CLI×MCP coverage (verbs named above), CHANGELOG,
RBAC gates, security playbook items above.

## Resolved review decisions

1. **Agent principals:** initial connect is human-only (`deny_principal`);
   server-side refresh during an agent-forwarded call is allowed (no user
   interaction involved; the owner connected the source deliberately).
2. **PKCE verifier storage:** DB table `mcp_oauth_flows` (works on
   single-replica DuckDB and multi-replica PG alike; no sticky sessions).
3. **Scope selection:** admin override field on the client row; defaults
   empty (AS/resource defaults).
4. **Egress knob:** no reuse of the ATTACH allowlist. The SSRF guard is
   always-on; a dedicated optional `mcp_oauth.host_allowlist` config key can
   further restrict issuers, but is not required for safety.
