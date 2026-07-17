# Per-user MCP credential onboarding — self-service connect page

Date: 2026-07-17
Status: design approved (revised after review), implementation pending

## Problem

A `scope='per_user'` MCP source fails closed when an identified caller has no
personal credential stored: `enforce_per_user_credential` raises
`PerUserCredentialMissing` rather than borrowing the shared credential. That
behaviour is correct and stays. The onboarding around it is not:

1. **The remedy is unreachable.** The message says to run
   `agnes mcp my-secret set <source>`. A user in web chat or Cowork has no
   shell — they cannot act on it. The only working path today is an operator
   storing the credential for them.
2. **There is no user-facing surface.** `PUT`/`DELETE`/`GET`
   `/api/mcp/sources/{source_id}/my-secret` exist, and so does the `agnes mcp
   my-secret` CLI, but there is no page. Users cannot connect, replace, verify,
   or remove their own credential.
3. **The agent cannot help.** When the tool call fails, the agent should say
   plainly "you are not connected, connect here" — that depends on the remedy
   text surviving, readably, to the model on every transport.

Net effect: per-user credentials — the mechanism that makes each caller see
only their own upstream data — are gated behind an operator.

## Goals

- When a caller with no personal credential invokes a per_user tool, the agent
  tells them in plain language that they are not connected and links them to a
  page that fixes it.
- Users self-serve the whole credential lifecycle: connect, replace, test,
  remove.
- Vendor-agnostic: the page and the message work for any per_user MCP source.

## Non-goals

Deferred, with the design leaving room for each:

- **OAuth / SSO connect flow.** See *Future: OAuth* below. This design assumes
  the upstream issues a personal token that the user pastes once.
- **Admin view of who is connected.** Per-caller surfaces only.
- **Upstream revocation.** Agnes can stop *using* a credential; only the
  upstream can revoke it. The page says so explicitly.
- **Expiry reminders / rotation policy.** `Test connection` covers the
  "is it still valid" question on demand.

## Design

### 1. Web page — `/me/connections`

A new page in the established `/me/*` namespace (alongside `/me/ai-connector`,
`/me/activity`). Behind `get_current_user`. Extends `base_page.html` (hero +
`{% block page %}`), page CSS in `{% block head_extra %}`, and **must spread the
chrome context** — a `base_page`/`base_ds` route that omits it renders with no
CSS and no nav while tests stay green.

**Which sources are listed.** Enabled `scope='per_user'` sources **that the
caller has a grant on** — not every per_user source. Every other passthrough
surface filters by grant intersection (`_visible_passthrough_tools`,
`app/api/mcp_passthrough.py`, intersects `tool_grants` with the caller's group
memberships), and grants are modelled per-tool: there is no source-level grant in
the schema. Derive the visible set as
`{tool["source_id"] for tool in <visible passthrough tools for caller>}`
intersected with `scope='per_user'`, rather than listing `mcp_sources` unfiltered.
Otherwise a source's name and its "where to get the token" hint become visible
platform-wide to users whose groups hold zero grants on any of its tools.
`scope='shared'` sources never appear: the user has nothing to do for them.

Each source is a card:

| State | Card contents |
|---|---|
| Not connected | status pill *Not connected*; password input; **Connect** (`PUT`); a hint on where to get the token |
| Connected | status pill *Connected*; *Updated `<timestamp>`*; **Replace token** (reveals input → `PUT`); **Test** (`POST …/my-secret/test`); **Remove** (`DELETE`, with confirmation) |

`PUT` is already documented as "store **/ rotate**", so *replace* needs no new
endpoint — only the affordance.

Deep link: `/me/connections?source=<id>` highlights and scrolls to that source.
This is the target of the agent's remedy message.

Copy requirements (sentence case, no first person):

- Page purpose: connect your own accounts so Agnes queries data as you; you see
  only what you are allowed to see.
- Security note: the token is encrypted, only used for your own requests,
  admins cannot see it, and it never passes through the chat.
- Removal semantics, stated plainly: **Remove stops Agnes from using the token.
  To fully revoke it, revoke it in the upstream system.** Agnes cannot revoke a
  credential it did not issue.

The token input is `type="password"`, `autocomplete="off"`. Cleartext is never
rendered back, never logged, never returned by any endpoint — rotation stays
write-only, as today.

### 2. Status endpoint gains `updated_at`

`GET /api/mcp/sources/{source_id}/my-secret` currently returns `has_secret` and
`source_scope`. Add `updated_at` (nullable) so the card can show when the
credential was last set. The `mcp_user_secrets` table already carries
`created_at` and `updated_at` on both ladders (`src/db.py` `_v65_to_v66`; Alembic
`0014_cowork_mcp_v63_v67.py`) — **no migration**. `upsert` already maintains
`updated_at` on rotation in both backends, so the displayed timestamp is accurate
after a `PUT`.

This needs a read method returning the metadata, added to **both backends in the
same change**:

- DuckDB: `PerUserSecretsRepository` — **in `app/secrets_vault.py`**, not
  `src/repositories/per_user_secrets.py`.
- Postgres: `PerUserSecretsPgRepository` in `src/repositories/secrets_vault_pg.py`.

Reach it through the `per_user_secrets_repo()` factory — never instantiate a repo
class directly.

**Ratchet gap — read this before assuming CI has your back.** The automatic
static parity sweep (`tests/db_pg/test_repo_method_parity.py`) scans only
`src/repositories/`. Because the DuckDB half of this cluster lives in
`app/secrets_vault.py`, **the sweep does not cover it** — a one-sided change will
not be caught mechanically. The same exception is documented for the sibling
cluster in `tests/db_pg/test_system_secrets_contract.py`. The manual cross-engine
test is therefore the *only* guard here.

The existing cross-engine test for this cluster is
`tests/db_pg/test_parity_mcp_user_secrets.py` (an HTTP-level test driving both
backends through a `TestClient`) — note it does **not** follow the
`test_<cluster>_contract.py` naming convention, so searching for that filename
finds nothing. Extend that file; do not create a new differently-named one.

### 3. Test connection — `POST /api/mcp/sources/{source_id}/my-secret/test`

Verifies the caller's stored credential actually works upstream.

**Route through the client, not the extractor.** The admin
`POST /api/admin/mcp-sources/{source_id}/test` uses
`introspect_source_async`, but there is already precedent for skipping it:
`classify_mcp_source` (`app/api/admin_mcp.py`) imports and calls
`connectors.mcp.client.list_tools_async` directly. The new endpoint does the
same. This keeps `connectors/mcp/extractor.py` **completely untouched** — its
`introspect_source_async`/`introspect_source` keep their current signature, and
the two admin endpoints that depend on their caller-less behaviour never need
re-review.

**The signature hop that must not be missed.** Unlike `call_tool_async`,
`list_tools_async` (and its sync sibling `list_tools`) currently has **no**
`caller_user_id` parameter at all — it calls `_open_session(source)` with
nothing. Add `caller_user_id: Optional[str] = None` to both, threaded to
`_open_session`, mirroring `call_tool_async`'s existing shape 1:1. Keep the
`Optional[str] = None` shape: it is the established convention across
`call_tool_async`, `_open_session`, `enforce_per_user_credential`, and
`enforce_passthrough_access`, and introducing a second "no caller" convention in
sibling functions of the same module would make it harder to read, not safer.

**Why the shape is a trap, and what actually closes it.**
`_lookup_secret_for_source(source, caller_user_id=None)` treats a *missing*
caller as the caller-less materialize path and legitimately resolves the
**shared** credential. So a forgotten parameter would test the shared credential
and report success to a user who is not connected. The gates below — not the
parameter default — are what prevent that. Note the residual honestly: for a user
who *has* a stored-but-stale credential, `enforce_per_user_credential` passes on
row presence alone, so a forgotten parameter would fall through to the shared
credential and report `ok: true`. A presence check cannot catch a plumbing
mistake; the mutation test in *Testing* is what guards it.

**Gates, in order, all before any upstream call:**

1. Source unknown → `404 mcp_source_not_found`.
2. **`source.scope != 'per_user'` → `400`.** Without this the endpoint is a live
   oracle: `enforce_per_user_credential` **no-ops for shared sources**, so any
   authenticated caller could target a shared source (the *default* scope) and
   have the introspection run under the operator's shared credential. A shared
   source has no "my credential" to test, so rejecting is also the honest
   semantics.
3. **Grant check.** Require that the caller's groups hold a `tool_grants` row for
   at least one tool on this source (the `enforce_passthrough_access`-equivalent
   the real invoke path applies). Without it the endpoint bypasses the grant gate
   that every other passthrough surface treats as load-bearing.
4. **Rate limit.** Reuse `check_rate_limit` with a `(source_id, user_id)` bucket.
   Each call opens a fresh connection — and for `transport='stdio'` spawns a
   subprocess — so an unthrottled caller can exhaust local resources and hammer
   the upstream into its own lockout.
5. `enforce_per_user_credential` → fail-closed `403` + remedy for an unconnected
   caller, with no upstream call at all.

**Response:** `{ok: bool, tool_count: int | null, message: str}`. Tool names and
schemas are not returned — a count is enough.

**Sanitizing the failure message.** The admin endpoint it resembles returns
`str(exc)` raw and untruncated; that is acceptable behind `require_admin` but not
here. For this endpoint, before returning *or logging*: truncate to a fixed
length, and **redact the caller's own token substring** — some upstreams echo the
presented credential back in a 401 body. The token is known server-side at error
time, so an exact-substring redact is cheap and closes the likely leak vector.
Strip internal host/port detail a lower-trust analyst would not otherwise see.

Per the sync-map, a new REST endpoint is **BLOCKING** on a CLI command and an
MCP tool that reach it:

- CLI: `agnes mcp my-secret test <source>` — joins the existing `set` / `clear`
  / `status` group. Per the command-UX standard: positional term, `--json`, and
  a "not found" error that hints the next step.
- MCP tool: exposed so an agent can run the check on the user's behalf.

### 4. Actionable remedy across every transport

`PerUserCredentialMissing` becomes web-first. When a public URL is configured
(`PUBLIC_URL` env / `server.public_url` YAML, via `instance_config`):

> You are not connected to '<source name>'. Open
> `<public_url>/me/connections?source=<source id>` and add your token, then try
> again.

When no public URL is configured, degrade to today's CLI hint rather than emit a
broken link. The message is built in one place so the transports cannot drift.

**The exception must carry the id, not just the label.** Today
`PerUserCredentialMissing` holds a single `source_label`, and the raise site
passes `source.get("name") or source["id"]` — i.e. it prefers the *name*. The
deep link needs the **primary key** (`mcp_sources_repo().get(source_id)` looks up
by id, not name), and putting a display name in a query string also leaks it into
browser history and referrers. Thread `source["id"]` through the exception as its
own field, separate from the human label used in the sentence.

The remedy is only useful if the model actually reads it. Ensure the text reaches
the agent readably on all four paths, fixing any that drop or flatten it:

1. REST passthrough → `HTTPException(403, detail=str(exc))`.
2. stdio `agnes mcp` → posts to the REST endpoint; the 403 body must surface in
   the raised error text, not collapse to an opaque HTTP failure.
3. SSE (`app/api/mcp_http.py`) → tool-error text.
4. Streamable-HTTP (`app/api/mcp_streamable.py`) → tool-error text.

No prompt engineering is needed beyond this: a self-describing error is enough
for the model to relay it.

## Flow

1. A user is signed in to Agnes but has no personal credential for a per_user
   source.
2. They ask the agent a question that needs it.
3. The agent calls the passthrough tool.
4. Gates pass on grant; `enforce_per_user_credential` fails closed with
   `PerUserCredentialMissing`.
5. The transport returns the actionable remedy text.
6. The agent relays it: not connected, plus the deep link.
7. The user opens `/me/connections?source=<id>` — already signed in, no second
   login.
8. They paste their token → **Connect** → `PUT` → encrypted into the vault under
   their own user id.
9. The card flips to *Connected*.
10. They ask again → the same tool → the per-user lookup finds their credential
    → the call forwards under their identity → the upstream applies its own
    per-identity access rules → they get their own data.
11. Any time: **Test** to verify it still works, **Replace token** after an
    upstream rotation, **Remove** to stop Agnes using it.

The token never passes through the chat or the agent. The user enters it only on
the Agnes page over TLS.

## Error handling

| Case | Behaviour |
|---|---|
| Vault key not configured | 409, existing message |
| Source id unknown | 404 `mcp_source_not_found` |
| Empty token on `PUT` | 400, existing message |
| Test on a `scope='shared'` source | 400 — nothing personal to test |
| Test without a grant on the source | 403 |
| Test over the rate limit | 429 + `Retry-After`, as the invoke path does |
| Test with no personal credential | 403 + the same remedy (no shared fallback) |
| Test upstream failure | `ok: false` + truncated, token-redacted message |
| Public URL unset | remedy degrades to the CLI hint |

## Testing

- Web page contract tests (`tests/test_design_system_contract.py` rules: extends
  `base_page.html`, CSS in `head_extra`, no raw hex, no `var(--primary)`), plus
  a route test asserting the page renders with chrome context (styled, nav
  present) — not merely HTTP 200.
- Page source list is grant-filtered: a user with no grant on a per_user source
  does not see that source (name or hint) at all.
- `GET …/my-secret` returns `updated_at`; null when not connected; reflects a
  rotation after a second `PUT`.
- Cross-engine coverage for the new repo read method — extend
  `tests/db_pg/test_parity_mcp_user_secrets.py`, both backends. This is the only
  guard for this cluster (see the ratchet gap in §2).
- Test endpoint gates, each asserting **no upstream call happens**: shared-scope
  source → 400; no grant → 403; over rate limit → 429; no personal credential →
  403 with the remedy.
- Test endpoint happy path → `ok: true` with a tool count.
- Test endpoint upstream failure → `ok: false`, message truncated and with the
  caller's token redacted (assert the token string does not appear in the
  response body or the log record).
- **Mutation test for the plumbing trap:** call the client function without
  `caller_user_id` against a `per_user` source *for a user who has a stored
  credential*, and assert it does not silently succeed off the shared credential.
  This covers the case `enforce_per_user_credential` structurally cannot.
- Remedy message: includes the deep link built from the source **id** when a
  public URL is set; falls back to the CLI hint when unset; per-transport
  propagation tests proving the text reaches the caller on REST, stdio, SSE, and
  Streamable.
- CLI + MCP tool parity for the new endpoint (triple-surface ratchet).
- Full suite before push: `.venv/bin/pytest tests/ --tb=short -n auto -q`.

## Sync-map impact

| Change | Mirror surface | Severity |
|---|---|---|
| New REST endpoint (`…/my-secret/test`) | CLI command + MCP tool | BLOCKING |
| New repo read method | `_pg` sibling + cross-engine test (manual — the static sweep does not reach `app/secrets_vault.py`) | BLOCKING |
| New web page | extends `base_page.html`, CSS in `head_extra`, chrome context | BLOCKING |
| User-visible behaviour | `## [Unreleased]` CHANGELOG bullet | BLOCKING |

No new `ResourceType`: every endpoint here is per-caller (`get_current_user`),
not admin-gated or entity-scoped. No migration: `updated_at` already exists on
both ladders.

## Future: OAuth

When an upstream supports an authorization-code flow, the same page is its
home: a source that declares an OAuth config renders **Connect with SSO** above
the manual field, and the callback stores the resulting token through the same
per-user secret path. The manual field stays as the fallback for upstreams that
only issue personal tokens. Neither the page contract nor the credential storage
model changes — only the card gains a button.
