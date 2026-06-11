# Data-source credentials via the admin UI — design

**Status:** proposal (not yet implemented)
**Date:** 2026-06-11

## Problem

Connecting Agnes to a data source today requires shell access to the VM:
the operator must SSH in, edit `.env` (`KEBOOLA_STORAGE_TOKEN`, or set up
`GOOGLE_APPLICATION_CREDENTIALS` for BigQuery), and restart the container.
Field feedback is consistent: this is the single sharpest edge of first-day
setup — admins who comfortably click through `/admin/tables` stall at
"now edit a dotfile on the VM". The admin UI can already edit non-secret
data-source settings (`/admin/server-config` → `data_source` section), so
the secret half of the configuration is the only part still requiring a
terminal.

**Goal:** an admin pastes the Keboola token / BigQuery service-account key
into a web form; it is encrypted at rest; sync and queries work without
anyone touching `.env`. Environment variables keep working and keep
priority, so Terraform/secret-manager-driven deployments are unaffected.

## What already exists (all of it gets reused)

| Piece | Where | State |
|---|---|---|
| Encrypted vault table `system_secrets` | `src/db.py` v72, `migrations/versions/0019_system_secrets_v72.py` | dual-backend, shipped |
| Fernet cipher + `AGNES_VAULT_KEY` handling | `app/secrets_vault.py` (`encrypt_secret`, `VaultKeyNotConfiguredError`) | shipped |
| `system_secrets_repo()` factory | `src/repositories/__init__.py` | dual-backend, shipped |
| Resolver precedent (env > vault > None, allow-list) | `services/slack_bot/secrets.py::slack_secret` | shipped |
| Write-only admin secrets API precedent | `app/api/admin_slack_secrets.py` (GET status / PUT / DELETE, never returns values, audits without params) | shipped |
| Admin secrets UI precedent | `app/web/templates/admin_slack_secrets.html` | shipped |
| Connection test endpoints | `app/api/admin_keboola_test.py`, `app/api/admin_bigquery_test.py` | shipped |

There is **no new table and no migration** in this design — the Slack
pattern is generalized to data sources.

## Current credential flow (what has to change)

**Keboola** — `KEBOOLA_STORAGE_TOKEN` (+ non-secret `KEBOOLA_STACK_URL`) is
read straight from `os.environ` at several sites:
`connectors/keboola/client.py:21-22`, `connectors/keboola/extractor.py:193,220`,
`connectors/keboola/metadata.py`, `app/api/admin.py:652`,
`app/api/sync.py:249-254`. Crucially, sync runs the extractor as a
**subprocess** with `env = {**os.environ}` (`app/api/sync.py:526`) — a vault
secret that only lives in the parent's DB connection never reaches it.

**BigQuery** — no stored secret at all today: `connectors/bigquery/auth.py`
tries the GCE metadata server, then gcloud ADC
(`GOOGLE_APPLICATION_CREDENTIALS` file path). Works great on a GCP VM with
an attached service account; on anything else the operator must provision a
key *file* on disk by hand. The orchestrator's `_remote_attach` path and the
materialize scheduler both funnel through this module already.

## Design

### 1. Resolver: `app/datasource_secrets.py`

Mirror of `slack_secret()` with its own allow-list:

```python
DATA_SOURCE_SECRET_NAMES = (
    "KEBOOLA_STORAGE_TOKEN",
    "BIGQUERY_SERVICE_ACCOUNT_JSON",   # full SA key JSON, not a file path
)

def datasource_secret(name: str) -> str | None:
    # env > vault > None; ValueError outside the allow-list;
    # vault lookup failure logged + treated as unset (fail toward env/manual).
```

Env-first is non-negotiable: deployments that inject secrets via
Terraform/Secret Manager stay authoritative, and the UI never silently
overrides them (the UI *shows* that env is the active source — see §4).

### 2. Subprocess env overlay (the Keboola fix)

In-process callers (admin test endpoint, metadata fetcher) switch from
`os.environ.get(...)` to `datasource_secret(...)` directly.

The extractor subprocess gets the overlay where sync builds its env
(`app/api/sync.py:526`):

```python
env = {**os.environ}
for name in DATA_SOURCE_SECRET_NAMES:
    if not env.get(name):
        value = datasource_secret(name)
        if value:
            env[name] = value
```

Only fills *unset* vars — never overrides env. The extractor itself keeps
reading `os.environ` and needs no change; per-table custom `token_env`
names (`data_source.keboola.token_env`) keep working because the overlay
fills the canonical name and env wins everywhere else.

### 3. BigQuery: a vault tier in `connectors/bigquery/auth.py`

New resolution order:

1. `GOOGLE_APPLICATION_CREDENTIALS` env (operator-pinned, unchanged)
2. **vault `BIGQUERY_SERVICE_ACCOUNT_JSON`** → `google.oauth2.service_account.Credentials.from_service_account_info(...)` — in-memory, the key is **never written to disk**
3. GCE metadata server (unchanged)
4. gcloud ADC (unchanged)

Vault sits above the metadata server deliberately: an admin who pastes a key
in the UI expects it to win over the VM's ambient identity (and on non-GCP
hosts tiers 3–4 don't exist anyway). Tier 1 stays first for parity with the
env-first rule everywhere else.

`PUT` validates the pasted JSON before storing: parses, requires
`type == "service_account"`, `private_key`, `client_email`; size-capped
(64 KiB). The orchestrator's `_remote_attach` re-attach, `agnes query
--remote`, materialize, and snapshot estimation all flow through `auth.py`
already, so they inherit the tier with no further changes.

### 4. API: `app/api/admin_datasource_secrets.py`

Clone of the Slack secrets router, `require_admin`-gated:

- `GET /api/admin/datasource-secrets` — per name: `{name, source: env|vault|unset, has_value}`. Never returns values.
- `PUT /api/admin/datasource-secrets/{name}` — write-only set/rotate; `400` outside the allow-list; `409 vault_key_not_configured` when `AGNES_VAULT_KEY` is missing; BQ JSON shape validation.
- `DELETE /api/admin/datasource-secrets/{name}` — clear vault row (resolution falls back to env/unset).
- `POST /api/admin/keboola/test`, `.../bigquery/test` (existing endpoints) switch to the resolver so "Test connection" exercises exactly what sync will use. Optionally accept a candidate value in the body to test *before* saving (value never persisted, never echoed).

Audit rows (`datasource.secret.set` / `.clear`) carry empty params — the
value never enters the audit log, mirroring the Slack/MCP endpoints.

### 5. UI: `/admin/datasource-credentials`

New page extending `base_ds.html` (design-system rules apply), modeled on
`admin_slack_secrets.html`, linked from the admin nav, from
`/admin/server-config`'s `data_source` section, and from the
`/admin/tables` empty state ("No tables? Connect your data source →").

Per-source card:

- **Keboola** — stack URL shown read-only with a link to
  `/admin/server-config` (single editing surface; no duplication), token as
  a write-only password input with a status badge (`env` / `vault` / `unset`
  — `env` renders the input disabled with "managed by deployment"), Save,
  Clear, **Test connection**.
- **BigQuery** — project/billing/location shown read-only (server-config
  link), SA-key JSON as a write-only textarea (cleared after save), status
  badge including which auth tier is currently active (env file / vault key
  / GCE metadata / ADC — surfaced by the test endpoint), Save, Clear,
  **Test connection**.

When `AGNES_VAULT_KEY` is unset, the page renders a blocking banner with the
one-liner to generate it and where to put it — the PUT would 409 anyway, so
say it up front.

### 6. Vault-key bootstrap (follow-up, separate PR)

The whole flow is gated on `AGNES_VAULT_KEY` existing. Today Slack secrets
have the same gate, so this design adds no new requirement — but to make
credentials-via-UI the *default* path, deployment templates should generate
the key at provision time (the JWT secret already has this pattern via
`app/secrets.py`'s overlay persistence). Tracked as a deployment-infra
follow-up, not part of this change.

## Security notes

- Secrets are write-only at the API surface; no endpoint ever returns one.
- Fernet (AES-128-CBC + HMAC) at rest; key never in the DB.
- The allow-list prevents the vault namespace from becoming an arbitrary
  env-var read/write channel.
- Decrypt failure after a key rotation → treated as unset + WARNING, so a
  rotated key degrades to "re-enter the token", never to a 500.
- BQ SA JSON lives only in memory at use time; no temp key files.

## Testing

- Unit: resolver precedence (env beats vault, vault beats unset, allow-list
  rejection), subprocess overlay fills-only-unset, BQ JSON validation, auth
  tier order (mock metadata/ADC).
- API: clone `tests/test_admin_*` Slack-secret tests for the new router
  (status 409 without vault key, write-only behavior, audit emission).
- Parity: GET route is parameter-free → picked up by the dynamic
  status-parity sweep automatically; `system_secrets` contract tests already
  cover both backends.
- E2E (manual, via `agnes-e2e-tester` checklists): paste token → test
  connection → register table → trigger sync → table lands in catalog,
  with `.env` containing no Keboola token.

## Rollout

1. **Phase 1 — Keboola token.** Resolver + sync subprocess overlay + router
   + UI card + test-connection rewire. Kills the sharpest onboarding edge.
2. **Phase 2 — BigQuery SA JSON.** Auth tier + validation + UI card.
3. **Phase 3 (optional) — generalize.** `JIRA_API_TOKEN`, SMTP password,
   etc. join the allow-list; the page grows a card per source. A CLI
   sibling (`agnes admin secrets set <name>`) can land here for the
   REST×CLI coverage row.

## Out of scope

- Per-user data-source credentials (the `mcp_user_secrets` pattern exists
  if ever needed; data sources are server-wide today).
- Vault key rotation tooling (`agnes admin vault rotate-key` — separate
  design).
- Moving non-secret config (stack URL, project IDs) — already editable via
  `/admin/server-config`.
- `ANTHROPIC_API_KEY` / E2B keys — chat-platform secrets with E2B-side
  delivery paths, deliberately not data-source credentials.
