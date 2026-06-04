# Slack bot tokens in the vault — design

**Date:** 2026-06-04
**Status:** Approved (brainstorm), pre-implementation
**Scope:** Make the three server-wide Slack bot secrets manageable from the admin UI via the encrypted secret vault, while keeping environment variables authoritative.

## Problem

The Slack bot integration reads its three secrets — `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_SIGNING_SECRET` — directly from `os.environ` at every use site (15 reads across `app/main.py`, `app/api/slack.py`, `services/slack_bot/sender.py`, `services/slack_bot/identity.py`, `services/slack_bot/socket_mode_client.py`). They are env-only by deliberate design: secrets must never leak into the `/admin/server-config` echo or get persisted as cleartext into the `instance.yaml` overlay (where `${ENV_VAR}` references are resolved on write).

That design is correct for the *config overlay*, but it leaves an operator with no way to set or rotate the bot tokens without SSH or Terraform. Meanwhile the platform already has an encrypted secret vault (Fernet, `app/secrets_vault.py`), now fully dual-backend through the repository factory (`shared_secrets_repo()` / `per_user_secrets_repo()` route on `use_pg()`; PG siblings in `src/repositories/secrets_vault_pg.py`). The vault is the right home for UI-managed secrets — encrypted at rest, write-only in the UI, never echoed.

The MCP vault scopes (`mcp_secrets`, `mcp_user_secrets`) are keyed by `source_id` and are semantically about MCP data sources, so the Slack bot tokens do not belong there.

## Goal

Resolve each Slack bot secret as **`env > vault > none`**:

1. **env** — if the environment variable is set, use it. Environment always wins, so Terraform / secret-manager-driven deployments are unaffected and an operator cannot accidentally override an env-pinned value from the UI.
2. **vault** — otherwise, read the value from a new `system_secrets` vault scope, settable from the admin UI.
3. **none** — if neither is present, behaviour is exactly as today: the feature stays disabled, fail-closed (Socket Mode logs the reason and does not start; signature verification rejects).

## Non-goals (YAGNI)

- **Not** a generic "manage any system secret from the UI" feature. The `system_secrets` table is generic, but the resolver and UI are hard-limited to the three known Slack token names via an allow-list. Extending to other secrets (SMTP password, OpenMetadata token, etc.) is a future, separate effort and needs no migration.
- **No** per-user Slack bot tokens — per-user vault secrets remain MCP-passthrough only.
- **No** vault-key rotation tooling — separate concern, unchanged here.
- **No** caching of vault reads (see Decisions).

## Architecture

Four layers, each mirroring the proven MCP vault pattern.

### 1. Storage — new `system_secrets` scope

New table, server-wide, keyed by secret name:

```
system_secrets(
  name              TEXT       PRIMARY KEY,   -- e.g. "SLACK_BOT_TOKEN"
  secret_value_enc  BLOB,                     -- Fernet ciphertext
  updated_at        TIMESTAMP
)
```

- **DuckDB:** new migration step `_v71_to_v72` in `src/db.py`; bump `SCHEMA_VERSION` 71 → 72.
- **Postgres:** matching Alembic migration `0019_system_secrets_v72.py`.
- Both ladders reach the same schema endpoint; `tests/test_db_schema_version.py` is the integration gate.
- Reuses the existing Fernet helpers `encrypt_secret` / `decrypt_secret` from `app/secrets_vault.py` — no new crypto.

### 2. Repositories (dual-backend, via factory)

Signature-compatible with the existing `SharedSecretsRepository`:

- `SystemSecretsRepository` (DuckDB) in `app/secrets_vault.py`:
  - `upsert(name, value)` — encrypt + ON CONFLICT replace.
  - `get(name) -> str | None` — decrypt; on `InvalidToken` (key rotated) log a warning and return `None` so the caller falls back to env / disabled (same tolerance as `SharedSecretsRepository.get`).
  - `delete(name)`, `has(name) -> bool`.
- `SystemSecretsPgRepository` in `src/repositories/secrets_vault_pg.py` — same signature, SQLAlchemy against the PG `system_secrets` table.
- Factory `system_secrets_repo()` in `src/repositories/__init__.py`, routing on `use_pg()` exactly like `shared_secrets_repo()`.
- Cross-engine contract test `tests/db_pg/test_system_secrets_contract.py` parametrizing both backends through one assertion set (upsert/get/has/delete, junk-decrypt → None).

### 3. Resolver — single accessor `env > vault > none`

New function `slack_secret(name: str) -> str | None` in `services/slack_bot/` (e.g. `services/slack_bot/secrets.py`):

```
ALLOWED = {"SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_SIGNING_SECRET"}

def slack_secret(name):
    assert name in ALLOWED            # never resolve arbitrary names via vault
    env = os.environ.get(name)
    if env:
        return env                    # env wins (Terraform override)
    return system_secrets_repo().get(name)   # vault, or None
```

- The **allow-list** prevents using the vault namespace to exfiltrate arbitrary environment variables.
- Replaces all 15 `os.environ.get("SLACK_…")` read sites with `slack_secret(...)`. This centralises resolution in one place.
- **No caching** (Decisions): each resolution checks env first (cheap); the vault DB-read + Fernet-decrypt only happens when env is unset *and* a vault value exists. `SLACK_SIGNING_SECRET` is read on every inbound HTTP Slack request, so deployments that put the signing secret in the vault pay one DB read + decrypt per request — accepted in exchange for zero added complexity and instant rotation.

### 4. Admin API + UI

New endpoints under `/api/admin/`, mirroring the MCP source secret control shipped in #530:

- `PUT /api/admin/slack-secrets/{name}` — set / rotate. Write-only: the value is accepted, never returned. Returns **409 `vault_key_not_configured`** when `AGNES_VAULT_KEY` is unset outside `LOCAL_DEV_MODE` (reuse `VaultKeyNotConfiguredError`). `name` must be in the allow-list, else **400**.
- `DELETE /api/admin/slack-secrets/{name}` — clear the vault value (falls back to env / disabled).
- `GET /api/admin/slack-secrets` — status only, never values: for each of the three names `{name, source: "env" | "vault" | "unset", has_value: bool}`. `source` is `env` when the env var is set (env wins), `vault` when only a vault row exists, `unset` when neither.

All gated by `require_admin`. Set/rotate/clear are audit-logged with the value masked (reuse `_SECRET_KEY_PATTERNS`).

**UI** — a "Slack" subsection on the existing `/admin/server-config` page (`app/web/templates/admin_server_config.html`), co-located with the transport selector (`chat.slack.transport`) that already lives there. Per token: a status badge (`env` / `vault` / `unset`) and a write-only set / rotate / clear control (input cleared after submit; 409 shown inline). When `source == "env"`, the control is shown read-only with an explanation that the value is pinned via the environment and cannot be overridden from the UI.

**Critical invariant:** the set/rotate control POSTs to the **vault endpoint**, never to the `instance.yaml` overlay write path. Secrets never touch the config overlay — the existing no-leak / no-cleartext-on-disk guarantees are preserved. (Two write targets coexist on one page: the rest of the form patches the yaml overlay; this section writes the vault. The UI must make that boundary clear in code structure.)

`GET /api/health` already reports `vault_key_configured`; the UI surfaces a hint when a vault secret is requested but the key is unset.

## Behaviour matrix

| env set | vault row | `source` | resolved value |
|---|---|---|---|
| yes | — | `env` | env value (vault ignored) |
| no | yes | `vault` | decrypted vault value |
| no | no | `unset` | `None` → feature disabled, fail-closed |
| no | yes but key rotated | `vault`* | `None` (junk-decrypt) → disabled |

\* status reports `vault` (a row exists) but resolution returns `None`; the UI hint about `vault_key_configured` covers the rotated-key case.

## Testing

- **Contract:** `tests/db_pg/test_system_secrets_contract.py` — both backends, full CRUD + junk-decrypt → None.
- **Resolver:** env-set → env wins (no DB read); env-unset + vault → vault value; neither → None; non-allow-listed name → never reaches the vault (assertion / rejection).
- **API:** set/rotate/clear/status happy paths; 409 on missing vault key; 400 on non-allow-listed name; `require_admin` gate; value never returned by any endpoint; audit entry masked.
- **Regression:** existing Slack tests that monkeypatch `os.environ["SLACK_…"]` must still pass unchanged — the accessor checks env first, so env-based tests are unaffected.
- **Schema:** `tests/test_db_schema_version.py` confirms DuckDB v72 and Alembic 0019 reach the same schema.

## Release

- New admin UI surface + new endpoints = **minor** version bump (precedent: #530 = 0.64.0). Single CHANGELOG `Added` bullet under `[Unreleased]`, plus the release-cut (version bump + CHANGELOG rename + new empty `[Unreleased]`) as the last commit on the PR.
- Docs to update: `docs/slack-manifest-http.md` and `docs/slack-manifest-socket.md` (note that tokens may be set via the admin UI/vault, not only env), the config-layering notes, and `config/.env.template` (env still wins; vault is the UI-managed fallback).

## Affected files (anticipated)

- `src/db.py` — `_v71_to_v72`, `SCHEMA_VERSION` 71→72.
- `migrations/versions/0019_system_secrets_v72.py` — new.
- `app/secrets_vault.py` — `SystemSecretsRepository`.
- `src/repositories/secrets_vault_pg.py` — `SystemSecretsPgRepository`.
- `src/repositories/__init__.py` — `system_secrets_repo()`.
- `services/slack_bot/secrets.py` — `slack_secret()` accessor + allow-list (new).
- Read-site swaps: `app/main.py`, `app/api/slack.py`, `services/slack_bot/sender.py`, `services/slack_bot/identity.py`, `services/slack_bot/socket_mode_client.py`.
- `app/api/admin.py` (or a small new router) — `/api/admin/slack-secrets` endpoints.
- `app/web/templates/admin_server_config.html` — Slack subsection.
- Tests: `tests/db_pg/test_system_secrets_contract.py`, resolver + API tests, CHANGELOG, docs.
