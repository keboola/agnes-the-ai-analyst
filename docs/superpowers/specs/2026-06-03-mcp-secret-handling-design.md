# MCP Source Secret Handling — Design

**Status:** approved scope, pending spec review
**Implementation scope:** Phase 1 + Phase 2 (this cycle). Phases 3–4 deferred (documented below as future work).

## Goal

Make the full MCP-source secret flow manageable from the **admin web UI** with the secret value stored in the **vault** (encrypted at rest), never requiring an operator to set a host environment variable — and make the vault key a safe, hard-to-footgun part of the model. A local analyst agent then uses the source through Agnes passthrough without ever holding the secret.

## Background — current state (verified)

The backend secret machinery already exists; the gaps are in the UI and operator safety.

- **Vault** (`app/secrets_vault.py`): Fernet (AES-128-CBC + HMAC) keyed by `AGNES_VAULT_KEY` (`_ENV_KEY_NAME`). `_get_fernet()` falls back to a process-ephemeral key with a WARNING when the env var is unset — secrets stored under it are unrecoverable after restart (silent footgun). `encrypt_secret(value)` / `decrypt_secret(token)` are the choke points.
- **Storage**: `mcp_secrets` (shared, one row per `source_id`) + `mcp_user_secrets` (per-user, `(source_id, user_id)`), both with DuckDB + PG repos via `SharedSecretsRepository` / `PerUserSecretsRepository` (encrypt at rest; `get`/`has`/`upsert`/`delete`).
- **Two coexisting secret models**: (A) **vault** — value encrypted in DB; (B) **legacy `auth_secret_env`** — the source row names a host env var whose value lives in the process environment, not the DB. Lookup precedence (`connectors/mcp/client.py::_lookup_secret_for_source`): per-user vault → shared vault → host env var. `env` (per-source non-secret vars, e.g. `CRM_API_URL`) was added in v0.63.0.
- **API**: `PUT/DELETE /api/admin/mcp-sources/{id}/secret` (shared, `require_admin`); `app/api/mcp_user_secrets.py` `PUT/DELETE/GET /api/mcp/sources/{id}/my-secret` (per-user, self). Values are write-only (never returned).
- **Gaps**: admin UI exposes only `auth_secret_env` (the host-env-var **name**) with misleading help text ("value not stored in DB"); no UI to store a vault secret value, no `env` field, no `scope` selector, no secret-status indicator; `AGNES_VAULT_KEY` is undocumented in `config/.env.template`; storing a secret with no key set silently uses the ephemeral key.

## Decisions (locked)

1. **Scope C** — broad redesign, delivered in phases. This cycle ships Phase 1 + 2.
2. **Legacy `auth_secret_env` kept but demoted** (non-breaking). Vault becomes the UI-primary path; the host-env-var path stays as an "Advanced (legacy)" option. Lookup precedence unchanged.
3. **`AGNES_VAULT_KEY` write-guard** — outside `LOCAL_DEV_MODE`, storing a secret when the key is unset is refused (rather than silently using the ephemeral key). Boot is NOT hard-failed (non-breaking for deployments that don't use the vault).

## Non-goals (this cycle)

- **Phase 3** — per-user secret web UI (analyst self-service for `per_user` sources). The `/my-secret` API exists; only the web surface is deferred.
- **Phase 4** — secret-coverage diagnostics dashboard + richer rotation audit.
- **No removal** of the legacy host-env path.
- **No schema change** — `mcp_secrets`, `mcp_user_secrets`, and `mcp_sources.env` already exist. (If anything tempts a schema change, stop and reconsider — this cycle must stay migration-free.)

---

## Phase 1 — Vault-key safety + model hygiene (backend + docs)

**Unit: vault write-guard** (`app/secrets_vault.py`)
- Add `vault_key_configured() -> bool` — true iff `AGNES_VAULT_KEY` is set to a valid Fernet key.
- Add exception `VaultKeyNotConfiguredError(RuntimeError)`.
- In the store path (`encrypt_secret`, the single choke point both admin and per-user writes funnel through): if **not** `is_local_dev_mode()` **and** `AGNES_VAULT_KEY` is unset → raise `VaultKeyNotConfiguredError("AGNES_VAULT_KEY must be set before storing secrets — otherwise they are lost on restart")`. The read path (`decrypt_secret`) is unchanged (still tolerant, still warns). `LOCAL_DEV_MODE` keeps the ephemeral convenience for local dev/tests.
- Interface contract: callers that store a secret may see `VaultKeyNotConfiguredError`; callers that read are unaffected.

**Unit: API error surfacing** (`app/api/admin_mcp.py::set_mcp_source_secret`, `app/api/mcp_user_secrets.py::set_my_secret`)
- Catch `VaultKeyNotConfiguredError` → HTTP 409 with `{"detail": "vault_key_not_configured: set AGNES_VAULT_KEY on the server before storing secrets"}`. Other behavior unchanged.

**Unit: health surface** (`app/api/health.py`)
- Add `vault_key_configured: bool` (from `vault_key_configured()`) to the health response so operators can pre-flight without storing a secret.

**Unit: docs**
- `config/.env.template`: add `AGNES_VAULT_KEY=` with a generation comment (`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`) alongside the existing `JWT_SECRET_KEY` / `SESSION_SECRET` block; note that without it MCP source secrets cannot be stored (outside local dev).
- Fix the misleading `auth_secret_env` help text in the admin UI templates (done structurally in Phase 2) to reflect that the vault stores values; the env-var path is the legacy/advanced alternative.

**Tests (Phase 1)**
- Storing a secret with `AGNES_VAULT_KEY` unset and `LOCAL_DEV_MODE` off → `VaultKeyNotConfiguredError` / API 409. With `LOCAL_DEV_MODE` on → succeeds (ephemeral). With key set → succeeds.
- `/health` reports `vault_key_configured` correctly for set/unset.

---

## Phase 2 — Admin UI: vault secret + env + scope (shared sources)

**Unit: source serialization** (`app/api/admin_mcp.py::_serialize_source`)
- Add `has_vault_secret: bool` (from `SharedSecretsRepository(conn).has(source_id)`) so the UI can show secret status without ever returning the value. (`env` is already serialized as of v0.63.0.)

**Unit: create/edit form** (`app/web/templates/admin_mcp_sources.html`, `admin_mcp_source_detail.html` + their inline JS)
- **`env` field**: a textarea of `KEY=VALUE` lines; JS parses to a `{VAR: value}` object and sends it as `env` in the POST/PUT body. Pre-fills from `source.env` on edit.
- **`scope` selector**: `shared` (default) | `per_user`; sends `scope`. (API already accepts it.)
- **Relabel `auth_secret_env`**: move under an "Advanced" disclosure, label "Host env var name (legacy)", help text: "Optional. Names a host environment variable holding the secret. Prefer the vault (below) — set the value directly so no host env var is needed."

**Unit: vault secret control** (`admin_mcp_source_detail.html` + JS)
- A write-only "Vault secret" control: a password-type input + **Set / rotate** button → `PUT /api/admin/mcp-sources/{id}/secret` with `{"value": ...}`; a **Clear** button → `DELETE .../secret`. The value is never displayed or fetched back.
- **Secret-status indicator** on the detail page and as a badge in the list (`admin_mcp_sources.html`): derived from `has_vault_secret` + `auth_secret_env` → one of "Vault secret set" / "Host env var (legacy)" / "No secret". On a 409 from the PUT (key not configured), show the actionable message inline.

**Data flow**
```
Admin UI  ──PUT /secret {value}──▶  SharedSecretsRepository.upsert ──encrypt_secret──▶ mcp_secrets (encrypted)
            (value write-only)        (raises if no key, !LOCAL_DEV_MODE)
Source spawn (passthrough)  ──_lookup_secret_for_source──▶ vault value + env{} → stdio subprocess env
Analyst agent  ──Agnes MCP passthrough──▶ never sees the secret or the CRM binary
```

**Tests (Phase 2)**
- `_serialize_source` includes `has_vault_secret` (true after a vault write, false after delete) — on both backends (extend the existing source serialization/contract coverage).
- Template render: create + detail pages contain the `env`, `scope`, and vault-secret controls; the legacy `auth_secret_env` help text no longer claims "not stored in DB". (Light template-content assertions, matching existing UI test style.)

---

## Future phases (deferred, not implemented this cycle)

- **Phase 3** — per-user secret web UI: an analyst-facing surface (e.g. on the source detail or a "my connections" page) to set/rotate/clear their own secret for `per_user`-scope sources via the existing `/api/mcp/sources/{id}/my-secret` endpoints; status `has_secret`, value write-only.
- **Phase 4** — admin secret-coverage view (which sources use vault / host-env / per-user / none) and a rotation audit trail surfaced in the UI (`secret.set` / `secret.delete` already audit-logged with actor + timestamp; no value).

## Delivery

Phases 1 + 2 ship as 1–2 small PRs off this spec (TDD, the rules + (if touched) architecture reviewers, full suite, release-cut). No schema migration. Version bump is **minor** (additive: new API field `has_vault_secret`, `/health` field, UI capability) — confirm minor vs patch at release-cut per the releaser policy.

## Cross-cutting invariants

- Secrets are **write-only** everywhere: never returned by any GET, never logged, never rendered. Status is a boolean.
- Admin gate (`require_admin`) on shared-secret writes; self-gate on per-user.
- Vendor-agnostic: no customer tokens/hostnames in code, templates, docs, or commit messages.
