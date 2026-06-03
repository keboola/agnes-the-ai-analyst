# MCP Secret Handling (Phases 1–2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Manage MCP-source secrets entirely from the admin UI (value stored in the Fernet vault, never a host env var), and refuse to silently store a secret when `AGNES_VAULT_KEY` is unset in production.

**Architecture:** Backend already has the vault (`app/secrets_vault.py`), the `mcp_secrets`/`mcp_user_secrets` repos, and the secret APIs. Phase 1 adds a write-guard at the vault's single encrypt choke point + surfaces key status; Phase 2 surfaces vault-secret + `env` + `scope` management in the existing admin source templates. No schema migration. Spec: `docs/superpowers/specs/2026-06-03-mcp-secret-handling-design.md`.

**Tech Stack:** Python 3.12, FastAPI, DuckDB+Postgres dual-backend (repos already exist), Jinja2 templates + vanilla inline JS, pytest.

---

## File map

- Modify: `app/secrets_vault.py` — add `vault_key_configured()`, `VaultKeyNotConfiguredError`, write-guard in `encrypt_secret`.
- Modify: `app/api/admin_mcp.py` — catch guard error → 409 in `set_mcp_source_secret`; add `has_vault_secret` to `_serialize_source` + callers.
- Modify: `app/api/mcp_user_secrets.py` — catch guard error → 409 in `set_my_secret`.
- Modify: `app/api/health.py` — add `vault_key_configured` to `/api/health`.
- Modify: `config/.env.template` — document `AGNES_VAULT_KEY`.
- Modify: `app/web/templates/admin_mcp_sources.html` — create form: `env` + `scope` + relabel legacy; list: secret-status badge.
- Modify: `app/web/templates/admin_mcp_source_detail.html` — edit form: `env` + `scope` + relabel; vault-secret set/rotate/clear control + status.
- Test: `tests/test_admin_mcp_vault.py` (extend), `tests/test_mcp_user_secrets.py` (extend), `tests/test_health*.py` or `tests/test_admin_mcp_vault.py` for the health field, plus a template-content test (new) `tests/test_admin_mcp_ui_fields.py`.

---

## Task 1: Worktree

- [ ] **Step 1: Confirm isolated worktree + branch**

Work is on branch `feat/mcp-secret-ui` (already created off `origin/main`, with the spec committed). Confirm: `git rev-parse --abbrev-ref HEAD` → `feat/mcp-secret-ui`. If not present, create it via `superpowers:using-git-worktrees` off `origin/main`. All commands run from the repo root.

---

## Task 2: Vault write-guard (Phase 1)

**Files:**
- Modify: `app/secrets_vault.py`
- Test: `tests/test_admin_mcp_vault.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_admin_mcp_vault.py`:

```python
def test_encrypt_secret_blocked_without_key_in_prod(monkeypatch):
    import app.secrets_vault as v
    monkeypatch.delenv("AGNES_VAULT_KEY", raising=False)
    monkeypatch.delenv("LOCAL_DEV_MODE", raising=False)
    v._reset_ephemeral_key_for_tests()
    assert v.vault_key_configured() is False
    with pytest.raises(v.VaultKeyNotConfiguredError):
        v.encrypt_secret("s3cr3t")


def test_encrypt_secret_allowed_in_local_dev_without_key(monkeypatch):
    import app.secrets_vault as v
    monkeypatch.delenv("AGNES_VAULT_KEY", raising=False)
    monkeypatch.setenv("LOCAL_DEV_MODE", "1")
    v._reset_ephemeral_key_for_tests()
    token = v.encrypt_secret("s3cr3t")           # ephemeral OK in local dev
    assert v.decrypt_secret(token) == "s3cr3t"


def test_encrypt_secret_allowed_with_key(monkeypatch):
    import app.secrets_vault as v
    from cryptography.fernet import Fernet
    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv("LOCAL_DEV_MODE", raising=False)
    v._reset_ephemeral_key_for_tests()
    assert v.vault_key_configured() is True
    assert v.decrypt_secret(v.encrypt_secret("s3cr3t")) == "s3cr3t"
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_admin_mcp_vault.py -k "encrypt_secret" -q`
Expected: FAIL — `vault_key_configured` / `VaultKeyNotConfiguredError` don't exist yet.

- [ ] **Step 3: Implement the guard**

In `app/secrets_vault.py`, after the `_ENV_KEY_NAME` definition add:

```python
class VaultKeyNotConfiguredError(RuntimeError):
    """Raised when a secret WRITE is attempted in a non-local-dev process
    that has no AGNES_VAULT_KEY set — storing under the ephemeral key would
    silently lose the secret on restart."""


def _is_local_dev_mode() -> bool:
    # Mirror app.auth.dependencies.is_local_dev_mode without importing it
    # (keeps app.secrets_vault free of an app.auth import edge).
    return os.environ.get("LOCAL_DEV_MODE", "").strip().lower() in ("1", "true", "yes")


def vault_key_configured() -> bool:
    """True iff AGNES_VAULT_KEY is set to a syntactically valid Fernet key."""
    raw = os.environ.get(_ENV_KEY_NAME, "").strip()
    if not raw:
        return False
    try:
        Fernet(raw.encode("ascii"))
        return True
    except (ValueError, InvalidToken):
        return False
```

Then change `encrypt_secret` to guard the write:

```python
def encrypt_secret(value: str) -> bytes:
    """Encrypt ``value`` and return ciphertext bytes (Fernet token).

    Refuses to encrypt under the ephemeral key outside LOCAL_DEV_MODE — a
    secret stored that way is lost on restart, so we fail loudly instead.
    """
    if not vault_key_configured() and not _is_local_dev_mode():
        raise VaultKeyNotConfiguredError(
            f"{_ENV_KEY_NAME} must be set before storing secrets — otherwise "
            "they are unrecoverable after restart."
        )
    return _get_fernet().encrypt(value.encode("utf-8"))
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/pytest tests/test_admin_mcp_vault.py -k "encrypt_secret" -q`
Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add app/secrets_vault.py tests/test_admin_mcp_vault.py
git commit -m "feat(vault): refuse secret writes without AGNES_VAULT_KEY outside local dev"
```

---

## Task 3: API 409 surfacing (Phase 1)

**Files:**
- Modify: `app/api/admin_mcp.py` (`set_mcp_source_secret`, ~line 492)
- Modify: `app/api/mcp_user_secrets.py` (`set_my_secret`, ~line 47)
- Test: `tests/test_admin_mcp_vault.py`, `tests/test_mcp_user_secrets.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_admin_mcp_vault.py` (uses the app test client + admin auth fixture already used in that file — mirror its existing client/auth setup):

```python
def test_set_secret_returns_409_without_vault_key(client_admin, monkeypatch, seeded_source_id):
    monkeypatch.delenv("AGNES_VAULT_KEY", raising=False)
    monkeypatch.delenv("LOCAL_DEV_MODE", raising=False)
    import app.secrets_vault as v; v._reset_ephemeral_key_for_tests()
    r = client_admin.put(f"/api/admin/mcp-sources/{seeded_source_id}/secret", json={"value": "x"})
    assert r.status_code == 409
    assert "vault_key_not_configured" in r.json()["detail"]
```

NOTE: reuse the existing fixtures in `tests/test_admin_mcp_vault.py` for an authenticated admin client and a seeded source id; if their names differ from `client_admin`/`seeded_source_id`, match the file's actual fixtures. If none exist, create the source via `POST /api/admin/mcp-sources` in the test first.

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_admin_mcp_vault.py -k "409" -q`
Expected: FAIL — currently the handler lets the error propagate as 500 (or stores under ephemeral).

- [ ] **Step 3: Implement**

In `app/api/admin_mcp.py`, import the error near the other `app.secrets_vault` import (line ~44): `from app.secrets_vault import SharedSecretsRepository, VaultKeyNotConfiguredError`. Wrap the upsert in `set_mcp_source_secret`:

```python
    try:
        SharedSecretsRepository(conn).upsert(source_id, body.value)
    except VaultKeyNotConfiguredError as exc:
        raise HTTPException(
            status_code=409,
            detail="vault_key_not_configured: set AGNES_VAULT_KEY on the server before storing secrets",
        ) from exc
```

In `app/api/mcp_user_secrets.py`, import `VaultKeyNotConfiguredError` from `app.secrets_vault` and wrap the `PerUserSecretsRepository(conn).upsert(...)` in `set_my_secret` the same way (same 409 + detail).

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/pytest tests/test_admin_mcp_vault.py tests/test_mcp_user_secrets.py -k "409 or my_secret or secret" -q`
Expected: PASS (new 409 tests + existing secret tests still green).

- [ ] **Step 5: Commit**

```bash
git add app/api/admin_mcp.py app/api/mcp_user_secrets.py tests/test_admin_mcp_vault.py tests/test_mcp_user_secrets.py
git commit -m "feat(mcp): 409 when storing a secret with no vault key configured"
```

---

## Task 4: /health vault_key_configured (Phase 1)

**Files:**
- Modify: `app/api/health.py` (`health_check`, ~line 332)
- Test: `tests/test_admin_mcp_vault.py`

- [ ] **Step 1: Write the failing test**

```python
def test_health_reports_vault_key_configured(client, monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
    assert client.get("/api/health").json()["vault_key_configured"] is True
    monkeypatch.delenv("AGNES_VAULT_KEY", raising=False)
    assert client.get("/api/health").json()["vault_key_configured"] is False
```

(Use the file's existing unauthenticated client fixture; `/api/health` needs no auth.)

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_admin_mcp_vault.py -k "health" -q`
Expected: FAIL — key absent from response.

- [ ] **Step 3: Implement**

In `app/api/health.py`, add `from app.secrets_vault import vault_key_configured` to the imports, and in `health_check()` change the return to include the field:

```python
    return {"status": status, "vault_key_configured": vault_key_configured(), **schema_check}
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/pytest tests/test_admin_mcp_vault.py -k "health" -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/api/health.py tests/test_admin_mcp_vault.py
git commit -m "feat(health): expose vault_key_configured"
```

---

## Task 5: Docs — .env.template (Phase 1)

**Files:**
- Modify: `config/.env.template`

- [ ] **Step 1: Add the entry**

In `config/.env.template`, near the existing `JWT_SECRET_KEY` / `SESSION_SECRET` block, add:

```
# Vault key for MCP-source secrets stored in the DB (Fernet, encrypted at rest).
# Generate: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Required to store MCP source secrets (outside LOCAL_DEV_MODE). Keep it stable —
# rotating it makes previously-stored secrets undecryptable.
AGNES_VAULT_KEY=
```

- [ ] **Step 2: Commit**

```bash
git add config/.env.template
git commit -m "docs(env): document AGNES_VAULT_KEY"
```

---

## Task 6: has_vault_secret serialization (Phase 2)

**Files:**
- Modify: `app/api/admin_mcp.py` (`_serialize_source` ~line 239; callers at ~395, ~411, ~454)
- Test: `tests/test_admin_mcp_vault.py`

- [ ] **Step 1: Write the failing test**

```python
def test_source_serialization_includes_has_vault_secret(client_admin, seeded_source_id, monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
    import app.secrets_vault as v; v._reset_ephemeral_key_for_tests()
    # before: no vault secret
    assert client_admin.get(f"/api/admin/mcp-sources/{seeded_source_id}").json()["has_vault_secret"] is False
    client_admin.put(f"/api/admin/mcp-sources/{seeded_source_id}/secret", json={"value": "tok"})
    assert client_admin.get(f"/api/admin/mcp-sources/{seeded_source_id}").json()["has_vault_secret"] is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_admin_mcp_vault.py -k "has_vault_secret" -q`
Expected: FAIL — key absent.

- [ ] **Step 3: Implement**

Change `_serialize_source` signature to accept the flag and include it:

```python
def _serialize_source(row: Dict[str, Any], *, has_vault_secret: bool = False) -> Dict[str, Any]:
    return {
        ...existing fields...,
        "has_vault_secret": has_vault_secret,
    }
```

At each caller, pass it from the vault repo (conn is in scope):
- list (`~395`): `return [_serialize_source(r, has_vault_secret=SharedSecretsRepository(conn).has(r["id"])) for r in rows]`
- get detail (`~411`): `out = _serialize_source(src, has_vault_secret=SharedSecretsRepository(conn).has(source_id))`
- update response (`~454`): `return _serialize_source(fresh, has_vault_secret=SharedSecretsRepository(conn).has(source_id)) if fresh else {"id": source_id}`

`SharedSecretsRepository` is already imported (line ~44).

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/pytest tests/test_admin_mcp_vault.py -k "has_vault_secret" -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/api/admin_mcp.py tests/test_admin_mcp_vault.py
git commit -m "feat(mcp): expose has_vault_secret on source serialization"
```

---

## Task 7: Admin UI — create/edit form fields (Phase 2)

**Files:**
- Modify: `app/web/templates/admin_mcp_sources.html` (create form + inline JS)
- Modify: `app/web/templates/admin_mcp_source_detail.html` (edit form + inline JS)
- Test: `tests/test_admin_mcp_ui_fields.py` (new)

First READ both templates fully to learn the existing field pattern (e.g. how `new-command` / `new-args` inputs are declared and how the create JS builds the POST body — `tests/`-free; just mirror it). Then:

- [ ] **Step 1: Write the failing test** (template content assertions — cheap, deterministic)

Create `tests/test_admin_mcp_ui_fields.py`:

```python
from pathlib import Path

TPL = Path("app/web/templates")


def _read(name):
    return (TPL / name).read_text()


def test_create_form_has_env_and_scope_and_legacy_label():
    html = _read("admin_mcp_sources.html")
    assert 'id="new-env"' in html              # env KEY=VALUE textarea
    assert 'id="new-scope"' in html            # scope selector
    assert "legacy" in html.lower()            # auth_secret_env relabelled as legacy/advanced
    # the misleading claim is gone
    assert "value itself is not stored in the db" not in html.lower()


def test_detail_form_has_env_scope_and_vault_secret_controls():
    html = _read("admin_mcp_source_detail.html")
    assert 'id="edit-env"' in html
    assert 'id="edit-scope"' in html
    assert 'id="set-vault-secret"' in html     # secret value input
    assert "/secret" in html                   # PUT/DELETE vault secret endpoint used by JS
    assert "legacy" in html.lower()
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_admin_mcp_ui_fields.py -q`
Expected: FAIL — none of those ids exist yet.

- [ ] **Step 3: Implement the form fields**

In `admin_mcp_sources.html` (create form), mirroring the existing `new-command`/`new-args` field block:
- Add a `scope` `<select id="new-scope">` with options `shared` (default) / `per_user`.
- Add an `env` `<textarea id="new-env" placeholder="CRM_API_URL=https://...&#10;ONE_PER_LINE=value">` with help text "Non-secret env vars passed to the stdio subprocess (KEY=VALUE per line)."
- Relabel the `new-auth-secret-env` field: move under an "Advanced (legacy)" heading, change its help text from the current "Name of the environment variable holding the secret value. The value itself is not stored in the DB." to: "Advanced/legacy: names a host env var holding the secret. Prefer the vault — set the secret value on the source detail page so no host env var is needed."
- In the create JS that builds `body`: parse `new-scope` → `body.scope`; parse `new-env` textarea into an object (`split("\n")`, each line `KEY=VALUE` → trim, skip blanks/those without `=`) → `body.env` (omit if empty).

In `admin_mcp_source_detail.html` (edit form): the same three changes with `edit-`-prefixed ids; pre-fill `edit-scope` from `source.scope`, `edit-env` from `Object.entries(source.env||{}).map(([k,v])=>k+"="+v).join("\n")`; include `scope` and parsed `env` in the PUT body.

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/pytest tests/test_admin_mcp_ui_fields.py -k "create_form or detail_form" -q`
Expected: the `env`/`scope`/`legacy` assertions PASS (the vault-secret control assertion is delivered in Task 8 — if running the full file, that one may still fail until Task 8).

- [ ] **Step 5: Commit**

```bash
git add app/web/templates/admin_mcp_sources.html app/web/templates/admin_mcp_source_detail.html tests/test_admin_mcp_ui_fields.py
git commit -m "feat(ui): env + scope fields on MCP source form; relabel legacy auth_secret_env"
```

---

## Task 8: Admin UI — vault secret control + status (Phase 2)

**Files:**
- Modify: `app/web/templates/admin_mcp_source_detail.html` (vault secret control + status + JS)
- Modify: `app/web/templates/admin_mcp_sources.html` (list secret-status badge)
- Test: `tests/test_admin_mcp_ui_fields.py`

- [ ] **Step 1: The test already asserts the detail controls** (from Task 7: `set-vault-secret`, `/secret`). Add a list-badge assertion:

```python
def test_list_shows_secret_status():
    html = _read("admin_mcp_sources.html")
    assert "has_vault_secret" in html          # list JS reads the flag to render a badge
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_admin_mcp_ui_fields.py -q`
Expected: the detail vault-secret + list-badge assertions FAIL.

- [ ] **Step 3: Implement**

In `admin_mcp_source_detail.html`:
- Add a "Vault secret" section with a password input `<input id="set-vault-secret" type="password" autocomplete="new-password">`, a **Set / rotate** button and a **Clear** button.
- JS: Set → `PUT /api/admin/mcp-sources/{id}/secret` with `{value}`; on `409` show the response `detail` inline ("set AGNES_VAULT_KEY…"); on 204 clear the input + refresh status. Clear → `DELETE .../secret`. Never read the value back.
- Status line derived from `source.has_vault_secret` (true → "Vault secret set"), else `source.auth_secret_env` (→ "Host env var (legacy): <name>"), else "No secret".

In `admin_mcp_sources.html` (list): render a per-row badge from `s.has_vault_secret` ("🔒 vault" / none) in the existing row-render JS.

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/pytest tests/test_admin_mcp_ui_fields.py -q`
Expected: all PASS.

- [ ] **Step 5: Manual render check (optional but recommended)**

Start a local instance (`LOCAL_DEV_MODE=1 DATA_DIR=$(mktemp -d) .venv/bin/uvicorn app.main:app --port 8021`), open `/admin/mcp-sources`, confirm the new fields render and a secret can be set/cleared with status updating. Stop the server after.

- [ ] **Step 6: Commit**

```bash
git add app/web/templates/admin_mcp_source_detail.html app/web/templates/admin_mcp_sources.html tests/test_admin_mcp_ui_fields.py
git commit -m "feat(ui): set/rotate/clear vault secret + secret-status on MCP source UI"
```

---

## Task 9: Full verification + reviewers

- [ ] **Step 1: Full suite**

Run: `.venv/bin/pytest tests/ --tb=short -n auto -q`
Expected: green. Known-unrelated local failures (the `e2b` import tests, occasional xdist-only perf/cache flakes) are acceptable if they reproduce on `origin/main`; everything you touched must pass. Clear any stale shared test DB first if a `mcp_sources`-column error appears: `rm -rf "${TMPDIR:-/tmp}/.agnes-test-data"` and any `/tmp/*/.agnes-test-data`.

- [ ] **Step 2: Reviewers**

Dispatch `agnes-reviewer-rules` (CHANGELOG/vendor-agnostic/AI-attribution/commit hygiene) and `agnes-reviewer-rbac` (the diff touches `app/api/` — confirm no new endpoint/gate is mis-gated and no new `ResourceType` is needed; expected: existing gates unchanged, `require_admin` on shared writes, self on per-user). Address blocking findings, re-run Step 1 if code changed. (Architecture reviewer is NOT triggered — no `extract.duckdb`/orchestrator/`src/db.py`/migration change.)

---

## Task 10: Changelog + release-cut

**Files:** `CHANGELOG.md`, `pyproject.toml`

- [ ] **Step 1: CHANGELOG bullet** under `## [Unreleased]` → `### Added`:

```markdown
- MCP source secrets are now fully manageable from the admin UI: set/rotate/clear a vault-stored secret (encrypted at rest), an `env` (KEY=VALUE) field and a `scope` selector on the source form, and a secret-status indicator — no host env var required. The legacy `auth_secret_env` (host-env) path is relabelled "Advanced (legacy)" and still works. Storing a secret with no `AGNES_VAULT_KEY` set now returns `409` (outside `LOCAL_DEV_MODE`) instead of silently using an ephemeral key; `/api/health` reports `vault_key_configured`. `AGNES_VAULT_KEY` is now documented in `config/.env.template`.
```

- [ ] **Step 2: Release-cut** — re-read `docs/RELEASING.md`; bump `pyproject.toml` to the next **minor** (additive: new API field + `/health` field + UI capability) unless the user said patch; rename `## [Unreleased]` → `## [X.Y.Z] — <date>` + fresh empty `## [Unreleased]`. Confirm minor-vs-patch with the user if unsure (releaser policy: ask before minor).

```bash
git add pyproject.toml CHANGELOG.md
git commit -m "chore(release): X.Y.Z"
```

---

## Task 11: PR (merge gated)

- [ ] **Step 1: Push + open PR** (`gh pr create`), vendor-agnostic body, no AI attribution. Summarize the why (full UI secret management, vault-key footgun guard) + test/review evidence + release-cut.
- [ ] **Step 2: STOP — do not merge.** Wait for explicit "mergni". After merge: tag + GitHub Release auto-fire via `release.yml`; confirm smoke-test green + rollback skipped.

---

## Self-Review

- **Spec coverage:** vault write-guard (Task 2), 409 surfacing both APIs (Task 3), `/health` field (Task 4), `.env.template` (Task 5), help-text fix (Task 7), `has_vault_secret` (Task 6), `env`+`scope`+legacy-relabel UI (Task 7), vault-secret control + status badge (Task 8). Phases 3–4 are explicitly out of scope. ✓
- **Placeholder scan:** backend steps carry exact code; UI steps carry exact element ids + the API contract + an explicit "read the template, mirror the existing field block" instruction (templates are pattern-heavy — full re-paste would be noise and is covered by the content-assertion tests). ✓
- **Consistency:** element ids (`new-env`/`edit-env`/`new-scope`/`edit-scope`/`set-vault-secret`), the `has_vault_secret` field name, and the 409 `vault_key_not_configured` detail are used identically across tasks and tests. ✓
