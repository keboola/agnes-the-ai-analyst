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

## Alternative considered — `.env_overlay` (rejected)

Agnes already has a UI-managed-secret mechanism: `app/secrets.py::persist_overlay_token()` writes a key to `${STATE_DIR}/.env_overlay` (chmod 0600) and sets `os.environ` live; startup loads the overlay via `os.environ.setdefault(...)` so real env (Terraform) wins. `ANTHROPIC_API_KEY`, `E2B_API_KEY`, the marketplace PAT, and the initial-workspace PAT all use it (e.g. `app/api/admin_chat.py::set_chat_secrets`). Routing the Slack tokens through it would need **zero** read-site changes and no DB work at all.

It was rejected here in favour of the vault because the vault gives **encryption at rest** (Fernet) and **live rotation without restart** (the `.env_overlay` Socket Mode token would need a restart, and values sit in plaintext on disk). The trade-off is a larger change surface; that cost is accepted deliberately. This is a conscious divergence from how the comparably-sensitive `ANTHROPIC_API_KEY` is handled today.

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
- **SQLAlchemy model (required, not optional).** A `SystemSecrets` ORM model must be declared (e.g. in `src/models/mcp.py` or a new `src/models/vault.py`) **and registered in `src/models/__init__.py`** — precedent: `MCPSecret` registered at `src/models/__init__.py`. Two gates depend on this:
  - `tests/db_pg/test_schema_parity.py::test_alembic_head_materializes_every_model` (#546) iterates `Base.metadata.sorted_tables`.
  - The DuckDB→PG state migrator (`scripts/migrate_duckdb_to_pg/__init__.py`) iterates `Base.metadata.sorted_tables` to build its copy task list. **If the table has no model, the migrator silently skips it and vault contents are lost on a DuckDB→Postgres migration.**
- **Non-`id` primary key.** `system_secrets` is keyed by `name`, not `id`. Add `"system_secrets": ["name"]` to `_PK_COLUMNS` in `scripts/migrate_duckdb_to_pg/__init__.py`, or `tests/db_pg/test_data_migration.py::test_non_id_pk_tables_are_in_pk_columns_map` fails.
- Both ladders reach the same schema endpoint; `tests/test_db_schema_version.py` is the integration gate.
- Reuses the existing Fernet helpers `encrypt_secret` / `decrypt_secret` from `app/secrets_vault.py` — no new crypto.

### 2. Repositories (dual-backend, via factory)

Signature-compatible with the existing `SharedSecretsRepository`:

- `SystemSecretsRepository` (DuckDB) in `app/secrets_vault.py`:
  - `upsert(name, value)` — encrypt + ON CONFLICT replace.
  - `get(name) -> str | None` — decrypt; catch **`(InvalidToken, RuntimeError)`** and return `None` (with a logged warning) so the caller falls back to env / disabled. `InvalidToken` = key rotated; `RuntimeError` = `AGNES_VAULT_KEY` set-but-malformed (raised by `_get_fernet()`). The existing `SharedSecretsRepository.get` only catches `InvalidToken`, so a malformed key would 500 there — we deliberately widen the catch here so a misconfigured key fails closed (feature disabled) instead of 500-ing every inbound Slack request.
  - `delete(name)`, `has(name) -> bool`.
- `SystemSecretsPgRepository` in `src/repositories/secrets_vault_pg.py` — same signature, SQLAlchemy against the PG `system_secrets` table.
- Factory `system_secrets_repo()` in `src/repositories/__init__.py`, routing on `use_pg()` exactly like `shared_secrets_repo()`.
- Cross-engine contract test `tests/db_pg/test_system_secrets_contract.py` parametrizing both backends through one assertion set (upsert/get/has/delete, junk-decrypt → None, malformed-key → None). **This contract test is mandatory, not optional:** the automatic method-parity sweep (`tests/db_pg/test_repo_method_parity.py`) only scans `src/repositories/*.py` paired with `*_pg.py`, so it will NOT see `SystemSecretsRepository` (which lives in `app/secrets_vault.py`, mirroring the existing `SharedSecretsRepository` placement). The contract test is the only mechanical guard against DuckDB/PG method drift for this repo.

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
- Replaces the **12** direct `os.environ.get("SLACK_…")` read sites with `slack_secret(...)`, centralising resolution in one place. Verified counts on current main (HEAD d237fc03):
  - `app/main.py` — 2 (`SLACK_APP_TOKEN`, `SLACK_BOT_TOKEN`), read at lines ~324–325 and passed into the `SocketModeDispatcher` constructor.
  - `app/api/slack.py` — 3 (`SLACK_SIGNING_SECRET`, once per endpoint).
  - `services/slack_bot/sender.py` — 6 (`SLACK_BOT_TOKEN`, once per send function).
  - `services/slack_bot/identity.py` — 1 (`SLACK_BOT_TOKEN`).
  - `services/slack_bot/socket_mode_client.py` — **0**: it receives `app_token`/`bot_token` via its constructor from `app/main.py`, it does not read env itself. It needs no change beyond what `app/main.py` already passes (now the resolved values).
- **Startup ordering.** `app/main.py` calls `resolve_bot_user_id()` (which now goes through `slack_secret()` → `get_system_db()`) inside the async `lifespan` startup. `get_system_db()` is synchronous and lazily runs `_ensure_schema()` on first call. The implementation must confirm `DATA_DIR` / state dir is available before the Slack-init block in `lifespan()` (it already is for other startup DB reads), so the first vault read at startup does not raise on a missing state dir.
- **No caching** (Decisions): each resolution checks env first (cheap); the vault DB-read + Fernet-decrypt only happens when env is unset *and* a vault value exists. `SLACK_SIGNING_SECRET` is read on every inbound HTTP Slack request, so deployments that put the signing secret in the vault pay one DB read + decrypt per request. This is the same DuckDB system-connection access pattern the MCP hot path already uses (read-only SELECT on the shared `system.duckdb` connection; safe under concurrent FastAPI threads) — accepted in exchange for zero added complexity and instant rotation.

### 4. Admin API + UI

New endpoints under `/api/admin/`, mirroring the MCP source secret control shipped in #530:

- `PUT /api/admin/slack-secrets/{name}` — set / rotate. Write-only: the value is accepted, never returned. Returns **409 `vault_key_not_configured`** when `AGNES_VAULT_KEY` is unset outside `LOCAL_DEV_MODE` (reuse `VaultKeyNotConfiguredError`). `name` must be in the allow-list, else **400**.
- `DELETE /api/admin/slack-secrets/{name}` — clear the vault value (falls back to env / disabled).
- `GET /api/admin/slack-secrets` — status only, never values: for each of the three names `{name, source: "env" | "vault" | "unset", has_value: bool}`. `source` is `env` when the env var is set (env wins), `vault` when only a vault row exists, `unset` when neither.

All gated by `require_admin` (imported from `app.auth.access`; it also hard-denies `SessionPrincipal` co-session tokens automatically, which the tests should assert). No new `ResourceType`/`ResourceTypeSpec` — server-wide admin-only secrets belong in the `require_admin` tier, matching the MCP secret endpoints which register none.

**Audit logging — the secret value never enters the audit record.** Set/rotate/clear emit an audit entry with an **empty params dict** (`{}`), carrying only the action name (e.g. `slack.secret.set` / `.clear`) and the token name as the resource ref — exactly as the MCP secret endpoints do (`_audit(conn, user_id, "mcp_source.secret.set", ref, {})`). Do **not** put the value in `params` and rely on masking: `app/api/admin.py`'s `_SECRET_KEY_PATTERNS` / `_mask` are private to that module and fire only on the YAML-overlay diff path — they are not a cross-cutting interceptor and are not reachable from a new router without an ill-advised private import. The request body key `"value"` is also **not** in `admin.py`'s `_SECRET_FIELDS` allow-list, so a naive `params={"value": body.value}` would persist the plaintext token. The correct, leak-proof pattern is the MCP one: empty params, value goes only to `system_secrets_repo().upsert(name, value)`.

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
- **API:** set/rotate/clear/status happy paths; 409 on missing vault key; 400 on non-allow-listed name; `require_admin` gate (including `SessionPrincipal` hard-deny); value never returned by any endpoint; audit entry carries empty params (assert the plaintext value is absent from the persisted audit row).
- **Regression:** existing Slack tests that monkeypatch `os.environ["SLACK_…"]` must still pass unchanged — the accessor checks env first, so env-based tests are unaffected.
- **Schema:** `tests/test_db_schema_version.py` confirms DuckDB v72 and Alembic 0019 reach the same schema; `tests/db_pg/test_schema_parity.py` (#546) confirms the new `SystemSecrets` model materialises at Alembic head; `tests/db_pg/test_data_migration.py` confirms the `name` PK is registered for the DuckDB→PG migrator.

## Release

- New admin UI surface + new endpoints = **minor** version bump. Current version is `0.65.18`, so the target is **`0.66.0`** (precedent: #530 was a comparable minor at 0.64.0). Single CHANGELOG `Added` bullet under `[Unreleased]`, plus the release-cut (version bump + CHANGELOG rename + new empty `[Unreleased]`) as the last commit on the PR.
- Docs to update: `docs/slack-manifest-http.md` and `docs/slack-manifest-socket.md` (note that tokens may be set via the admin UI/vault, not only env), the config-layering notes, and `config/.env.template` (env still wins; vault is the UI-managed fallback).

## Affected files (anticipated)

- `src/db.py` — `_v71_to_v72`, `SCHEMA_VERSION` 71→72.
- `migrations/versions/0019_system_secrets_v72.py` — new Alembic migration.
- `src/models/mcp.py` (or new `src/models/vault.py`) — `SystemSecrets` ORM model.
- `src/models/__init__.py` — register the new model (so it lands in `Base.metadata`).
- `scripts/migrate_duckdb_to_pg/__init__.py` — add `"system_secrets": ["name"]` to `_PK_COLUMNS`.
- `app/secrets_vault.py` — `SystemSecretsRepository`.
- `src/repositories/secrets_vault_pg.py` — `SystemSecretsPgRepository`.
- `src/repositories/__init__.py` — `system_secrets_repo()` factory.
- `services/slack_bot/secrets.py` — `slack_secret()` accessor + allow-list (new).
- Read-site swaps (12 reads): `app/main.py` (2), `app/api/slack.py` (3), `services/slack_bot/sender.py` (6), `services/slack_bot/identity.py` (1). `socket_mode_client.py` unchanged (receives tokens via constructor).
- `app/api/admin.py` (or a small new router) — `/api/admin/slack-secrets` endpoints, audit with empty params.
- `app/web/templates/admin_server_config.html` — Slack subsection.
- Tests: `tests/db_pg/test_system_secrets_contract.py` (mandatory — not covered by the auto method-parity sweep), resolver + API tests, CHANGELOG, docs.
