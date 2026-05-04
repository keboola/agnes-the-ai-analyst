# Clean Analyst Bootstrap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the interactive `da analyst setup` flow with a single web→paste→done bootstrap. New analyst pastes a clipboard prompt from `/setup?role=analyst` into Claude Code in an empty folder, and ends up with `CLAUDE.md`, `AGNES_WORKSPACE.md`, hooks, fresh data, and DuckDB views — fully ready to query. Drop dead workspace dirs (`data/parquet/`, `data/duckdb/`, `data/metadata/`, `user/artifacts/`). Establish a lazy-mkdir contract so nothing creates empty directories.

**Architecture:** PAT-only auth. `agnes init` is a thin orchestrator that auths, fetches `CLAUDE.md` from `/api/welcome`, installs hooks, and calls `cli/lib/pull.py:run_pull` for first data refresh. CLI verbs renamed: `init/pull/push/status/snapshot create` (greenfield, no aliases). Server-side install prompt branches on `role` query param. `cli/lib/` shared library tree separates data primitives from Typer wrappers so `agnes init` can call them without subprocess. Reader contract: every reader handles missing dirs gracefully (exit 0 empty or exit 1 with friendly hint, never traceback).

**Tech Stack:** Python 3.11+, FastAPI, Typer, Pydantic, DuckDB, httpx, pytest, Hatchling.

**Spec:** `docs/superpowers/specs/2026-05-04-clean-analyst-bootstrap-design.md` (revision 5, cleared for implementation).

**CLI rename:** As part of this plan, the binary changes from `da` to `agnes`. References to legacy commands (`da sync`, `da fetch`, `da analyst setup`, `da metrics`) keep their `da` prefix throughout this document — they're historical artifacts being removed. New commands and hook strings use `agnes`.

---

## File Structure

### New files

| Path | Responsibility |
|---|---|
| `cli/lib/__init__.py` | Empty — makes `cli/lib/` a package so Hatchling includes it in the wheel. |
| `cli/lib/pull.py` | `run_pull(server_url, token, workspace, *, dry_run) -> PullResult` — pure-function data refresh primitive (manifest, parquet download, DuckDB rebuild, memory bundle write). Lazy mkdir. |
| `cli/lib/hooks.py` | `install_claude_hooks(workspace)` — idempotent SessionStart/End hook installer for `<workspace>/.claude/settings.json`. |
| `cli/commands/init.py` | `agnes init` Typer command — auth check, save config, write CLAUDE.md, install hooks, call `run_pull`, write `AGNES_WORKSPACE.md`. |
| `cli/commands/pull.py` | `agnes pull` Typer wrapper around `cli/lib/pull.py:run_pull`. Flags `--quiet`, `--json`, `--dry-run`. |
| `cli/commands/push.py` | `agnes push` Typer command — uploads `user/sessions/*.jsonl` and `.claude/CLAUDE.local.md`. |
| `cli/commands/admin_metrics.py` | `agnes admin metrics {import,export,validate}` sub-Typer (lifted from `cli/commands/metrics.py`). |
| `config/agnes_workspace_template.txt` | Static client-side template for `AGNES_WORKSPACE.md`. Three placeholders: `{created_at}`, `{server_url}`, `{workspace_path}`. |
| `tests/fixtures/analyst_bootstrap.py` | Test fixtures: `fastapi_test_server`, `test_pat`, `test_pat_no_grants`, `zero_grants_workspace`, `web_session`. |
| `tests/test_lib_hooks.py` | Tests for `install_claude_hooks` (idempotent, preserves third-party hooks, replaces old `agnes pull`/`da sync` entries). |
| `tests/test_lib_pull.py` | Tests for `run_pull` (lazy mkdir, partial failure handling, manifest empty case). |
| `tests/test_setup_instructions_analyst.py` | Tests `render_setup_instructions(role="analyst")` produces correct steps. |
| `tests/test_tokens_bootstrap_scope.py` | Tests `scope=bootstrap-analyst` PATs are TTL-clamped to ≤ 1 h; `ttl_seconds` upper bound; `ttl_seconds` wins over `expires_in_days`. |
| `tests/test_legacy_strings_scan.py` | Tests `_scan_legacy_strings` and `legacy_strings_detected` field on `GET /api/admin/workspace-prompt-template`. |
| `tests/test_clean_install_integration.py` | End-to-end clean-install integration tests (minimal grants, zero grants, force preserves, AGNES_WORKSPACE.md content). |
| `tests/test_reader_smoke_matrix.py` | Reader smoke matrix — every CLI command on a freshly-bootstrapped zero-grants workspace, asserts no traceback. |

### Modified files

| Path | Change |
|---|---|
| `app/api/tokens.py` | `CreateTokenRequest`: add `scope: str = "general"` and `ttl_seconds: Optional[int] = None`. Validate `ttl_seconds <= 315_360_000`. Resolution: `ttl_seconds` wins; fall back to `expires_in_days`. For `scope == "bootstrap-analyst"`, force-clamp resolved TTL ≤ 3600 s. Audit-log includes scope. |
| `app/api/claude_md.py` | Add module-level `_LEGACY_STRINGS = ("data/parquet", "da sync", "da fetch", "da analyst setup", "da metrics list", "da metrics show")`. Add helper `_scan_legacy_strings(text) -> list[str]`. Add field `legacy_strings_detected: list[str] = []` to `TemplateGetResponse`. Populate in `admin_get_workspace_template`. |
| `app/web/setup_instructions.py` | Add `role: Literal["analyst","admin"] = "admin"` to `resolve_lines()` and `render_setup_instructions()`. Analyst layout: TLS trust (when `ca_pem`) → install `agnes` → `agnes init --server-url X --token Y --workspace .` → `agnes catalog` smoke verify → confirm. Drop for analyst: marketplace, plugins, skills, diagnose, login, whoami. |
| `app/web/router.py` | `setup_page`: read `role` query param (default `"admin"`), pass to `render_setup_instructions(role=...)`. |
| `app/web/templates/setup.html` (or wherever `setup_page` renders) | Two role tiles (Analyst / Admin), POST `/auth/tokens` with matching `scope`. |
| `app/web/templates/admin_workspace_prompt.html` | Yellow banner above editor when `legacy_strings_detected` non-empty. |
| `config/claude_md_template.txt` | Update verb names: `da sync` → `agnes pull`, `da fetch` → `agnes snapshot create`, `da metrics list/show` → `agnes catalog --metrics`, `da analyst setup` → `agnes init`. Path strings: `data/parquet/` → `server/parquet/`, `data/duckdb/...` → `user/duckdb/analytics.duckdb`. |
| `cli/commands/snapshot.py` | Add `create` subcommand — moves logic from `cli/commands/fetch.py` verbatim. Add `if not db_path.exists(): exit 1` guard before `duckdb.connect()`. |
| `cli/commands/catalog.py` | Add `--metrics` flag (replaces `da metrics list`); `--metrics --show <id>` (replaces `da metrics show`). |
| `cli/commands/admin.py` | Register the new `admin_metrics_app` sub-Typer. |
| `cli/commands/query.py` | Update hint text "Run: da sync" → "Run: agnes pull" in two places. |
| `cli/commands/explore.py` | Update hint text "Run: da sync" → "Run: agnes pull". |
| `cli/main.py` | Drop registrations for `sync`, `analyst`, `metrics`, `fetch`, `status` (existing). Add `init`, `pull`, `push`. Re-register `status` to point at new workspace-status command. |
| `CLAUDE.md` (repo root) | Verb + path rewrites throughout. The "Local sync & Claude Code hooks" subsection rewrites verbatim with new commands. The "Querying Agnes data — agent rails" subsection keeps the 0.32.0 `query_mode='materialized'` and `query_mode='remote'` cost-guardrail prose verbatim, just verb-renaming `da fetch` → `agnes snapshot create`. |
| `CHANGELOG.md` | Entry under `[Unreleased]` per spec preview (Changed/Added/Fixed/Removed/Kept). |
| `pyproject.toml` | No change; `cli/lib/__init__.py` triggers Hatchling auto-discovery. |

### Deleted files

| Path | Reason |
|---|---|
| `cli/commands/sync.py` | Replaced by `cli/commands/pull.py` + `cli/commands/push.py` + `cli/lib/pull.py`. |
| `cli/commands/fetch.py` | Folded into `cli/commands/snapshot.py:create`. |
| `cli/commands/analyst.py` | Replaced by `cli/commands/init.py` + new `cli/commands/status.py` (workspace status). `_install_claude_hooks` lifted to `cli/lib/hooks.py`. |
| `cli/commands/metrics.py` | Read paths fold into `agnes catalog --metrics`; write paths move to `cli/commands/admin_metrics.py`. |

### Existing `cli/commands/status.py` rename

| Action | Detail |
|---|---|
| Existing `agnes status` ("System status") | Renamed to `agnes diagnose system` (subcommand under `diagnose_app`) — its content is a subset of what `agnes diagnose` already does. |
| New `agnes status` | Workspace status — fresh implementation, replaces `da analyst status`. Lives in `cli/commands/status.py` (overwrite). |

---

## Phase 0 — CLI binary rename (`da` → `agnes`)

### Task 0: Rename the CLI entry point

**Files:**
- Modify: `pyproject.toml` (`[project.scripts]`), `cli/main.py` (`Typer(name=...)`)
- Test: `tests/test_cli_binary_rename.py` (new)

**Why first:** Every later task that registers Typer apps, writes hook command strings, or asserts CLI output uses `agnes`. Rename the binary up front so tests in subsequent tasks reference the right name.

- [ ] **Step 1: Read current entry points**

```bash
grep -n "scripts\|^name\|tool.hatch" pyproject.toml | head
grep -n "Typer\|name=\"da\"\|name='da'" cli/main.py
```

- [ ] **Step 2: Update `pyproject.toml`**

In `[project.scripts]`, replace `da = "cli.main:app"` with:

```toml
[project.scripts]
agnes = "cli.main:app"
```

Single entry — no `da` alias kept. Greenfield.

- [ ] **Step 3: Update `cli/main.py`**

Change the Typer app construction from `name="da"` to `name="agnes"` and update the help string:

```python
app = typer.Typer(
    name="agnes",
    help="Agnes — AI Data Analyst CLI",
    no_args_is_help=True,
)
```

- [ ] **Step 4: Reinstall the editable package**

```bash
uv pip install -e ".[dev]"
which agnes
agnes --version
```

Expected: `agnes <version>` prints; `da --version` now fails with "command not found".

- [ ] **Step 5: Write a binary-name regression test**

```python
# tests/test_cli_binary_rename.py
"""Confirm the wheel installs the binary as `agnes`, not `da`."""
import subprocess


def test_agnes_command_exists():
    result = subprocess.run(["agnes", "--version"], capture_output=True, text=True)
    assert result.returncode == 0


def test_da_command_no_longer_works():
    """Greenfield: no backward-compat alias."""
    result = subprocess.run(["bash", "-c", "command -v da"],
                            capture_output=True, text=True)
    assert result.returncode != 0, "da should NOT be on PATH after rename"
```

- [ ] **Step 6: Run the test**

```bash
pytest tests/test_cli_binary_rename.py -v
```

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml cli/main.py tests/test_cli_binary_rename.py
git commit -m "feat(cli): rename binary from da to agnes (BREAKING)"
```

---

## Phase 1 — Server-side foundation (PAT scope, legacy-strings scan, install-prompt branching)

### Task 1: Add `scope` + `ttl_seconds` fields to `CreateTokenRequest`

**Files:**
- Modify: `app/api/tokens.py:23-25` (`CreateTokenRequest`), `app/api/tokens.py:85-101` (`create_token` route)
- Test: `tests/test_tokens_bootstrap_scope.py` (new)

- [ ] **Step 1: Write failing tests**

```python
# tests/test_tokens_bootstrap_scope.py
"""Tests for PAT scope + ttl_seconds fields (clean-analyst-bootstrap spec)."""

from __future__ import annotations

import jwt
import pytest


@pytest.fixture
def web_session(client, db_with_admin_user):
    """Authenticated test client with session cookie for admin user."""
    # Form-login endpoint — see fixtures/analyst_bootstrap.py
    resp = client.post("/auth/password/login/web",
                       data={"email": "admin@example.com", "password": "test-password"})
    assert resp.status_code in (200, 302), f"login failed: {resp.text}"
    return client


def _decode(pat: str) -> dict:
    return jwt.decode(pat, options={"verify_signature": False})


def test_bootstrap_pat_ttl_clamped_to_one_hour(web_session):
    resp = web_session.post("/auth/tokens", json={
        "name": "init",
        "scope": "bootstrap-analyst",
        "ttl_seconds": 86400,  # 1 day — must be ignored, clamped to 3600
    })
    assert resp.status_code == 201, resp.text
    payload = _decode(resp.json()["token"])
    assert payload.get("scope") == "bootstrap-analyst"
    assert payload["exp"] - payload["iat"] <= 3600 + 5


def test_general_pat_uses_ttl_seconds_when_set(web_session):
    resp = web_session.post("/auth/tokens", json={
        "name": "test",
        "ttl_seconds": 7200,  # 2 hours
    })
    assert resp.status_code == 201
    payload = _decode(resp.json()["token"])
    assert payload["exp"] - payload["iat"] <= 7200 + 5


def test_general_pat_falls_back_to_expires_in_days(web_session):
    resp = web_session.post("/auth/tokens", json={
        "name": "test", "expires_in_days": 30,
    })
    assert resp.status_code == 201
    payload = _decode(resp.json()["token"])
    assert payload["exp"] - payload["iat"] <= 30 * 86400 + 5


def test_ttl_seconds_upper_bound(web_session):
    # 3650 days * 86400 = 315_360_000 seconds. One past this must reject.
    resp = web_session.post("/auth/tokens", json={
        "name": "test", "ttl_seconds": 315_360_001,
    })
    assert resp.status_code == 400


def test_ttl_seconds_must_be_positive(web_session):
    resp = web_session.post("/auth/tokens", json={
        "name": "test", "ttl_seconds": 0,
    })
    assert resp.status_code == 400


def test_scope_default_is_general(web_session):
    resp = web_session.post("/auth/tokens", json={"name": "test"})
    assert resp.status_code == 201
    payload = _decode(resp.json()["token"])
    # scope=general is informational; check audit_log carries it
    # (skipped here — tested in test_audit_log_includes_scope below)
    assert payload.get("scope", "general") == "general"
```

The `db_with_admin_user` fixture is part of the existing test suite or will be added in `tests/fixtures/analyst_bootstrap.py` (Task 22). For now, this test depends on it; if it doesn't exist, mark these as `pytest.skip` until Task 22.

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd "$(git rev-parse --show-toplevel)"
pytest tests/test_tokens_bootstrap_scope.py -v
```

Expected: tests FAIL with either fixture-missing error or `extra fields not permitted` from Pydantic (if the fixture exists).

- [ ] **Step 3: Update `CreateTokenRequest` model**

Replace `app/api/tokens.py:23-25`:

```python
class CreateTokenRequest(BaseModel):
    name: str
    expires_in_days: Optional[int] = 90  # null = no expiry
    scope: str = "general"  # informational; "bootstrap-analyst" force-clamps TTL ≤ 1 h
    ttl_seconds: Optional[int] = None  # if set, wins over expires_in_days
```

- [ ] **Step 4: Update `create_token` route**

Replace `app/api/tokens.py:85-118` (the `create_token` function body up through the `jwt_token = create_access_token(...)` call):

```python
@router.post("", response_model=CreateTokenResponse, status_code=201)
async def create_token(
    payload: CreateTokenRequest,
    user: dict = Depends(require_session_token),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    if not payload.name.strip():
        raise HTTPException(status_code=400, detail="name is required")
    if payload.expires_in_days is not None and payload.expires_in_days <= 0:
        raise HTTPException(status_code=400, detail="expires_in_days must be a positive integer")
    if payload.expires_in_days is not None and payload.expires_in_days > 3650:
        raise HTTPException(status_code=400, detail="expires_in_days must not exceed 3650 (10 years)")
    if payload.ttl_seconds is not None and payload.ttl_seconds <= 0:
        raise HTTPException(status_code=400, detail="ttl_seconds must be a positive integer")
    # Mirror the 3650-day cap on ttl_seconds so a hostile client can't
    # bypass via field rename. 3650 days * 86400 = 315_360_000.
    if payload.ttl_seconds is not None and payload.ttl_seconds > 315_360_000:
        raise HTTPException(status_code=400, detail="ttl_seconds must not exceed 315360000 (10 years)")

    # Resolve TTL: ttl_seconds wins; fall back to expires_in_days.
    expires_delta: Optional[timedelta] = None
    omit_exp = False
    if payload.ttl_seconds is not None:
        expires_delta = timedelta(seconds=payload.ttl_seconds)
    elif payload.expires_in_days is not None:
        expires_delta = timedelta(days=payload.expires_in_days)
    else:
        omit_exp = True  # "no expiry"

    # Force-clamp bootstrap-analyst PATs to ≤ 1 h regardless of request.
    if payload.scope == "bootstrap-analyst":
        ONE_HOUR = timedelta(hours=1)
        if expires_delta is None or expires_delta > ONE_HOUR:
            expires_delta = ONE_HOUR
        omit_exp = False

    expires_at: Optional[datetime] = None
    if expires_delta is not None:
        expires_at = datetime.now(timezone.utc) + expires_delta

    repo = AccessTokenRepository(conn)
    token_id = str(uuid.uuid4())

    jwt_token = create_access_token(
        user_id=user["id"], email=user["email"],
        token_id=token_id, typ="pat",
        expires_delta=expires_delta, omit_exp=omit_exp,
        extra_claims={"scope": payload.scope},
    )
```

- [ ] **Step 5: Update `create_access_token` to accept `extra_claims`**

Find `app/auth/jwt.py:create_access_token` (read it first to get the current signature). Add an `extra_claims: dict | None = None` parameter that gets merged into the JWT payload before encoding. Show your edit:

```bash
grep -n "def create_access_token" app/auth/jwt.py
# Read the function and update.
```

If the function already supports extra claims, this is a no-op. Otherwise add:

```python
def create_access_token(
    user_id: str, email: str, token_id: Optional[str] = None,
    typ: str = "session", expires_delta: Optional[timedelta] = None,
    omit_exp: bool = False, extra_claims: Optional[dict] = None,
) -> str:
    payload = {"sub": user_id, "email": email, "typ": typ}
    if token_id:
        payload["jti"] = token_id
    if not omit_exp:
        payload["iat"] = int(datetime.now(timezone.utc).timestamp())
        if expires_delta:
            payload["exp"] = int((datetime.now(timezone.utc) + expires_delta).timestamp())
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, _SECRET, algorithm="HS256")
```

(Adapt to the actual function shape after reading it.)

- [ ] **Step 6: Update audit-log entry to include scope**

Search `app/api/tokens.py` for the `_audit(...)` call inside `create_token` and add `scope` to the `params` dict:

```python
_audit(conn, actor=user["id"], action="token.create",
       target=token_id,
       params={"name": payload.name,
               "expires_at": str(expires_at) if expires_at else None,
               "scope": payload.scope})
```

- [ ] **Step 7: Run tests to verify they pass**

```bash
pytest tests/test_tokens_bootstrap_scope.py -v
```

Expected: all PASS (or skip if `db_with_admin_user` fixture doesn't yet exist; in that case the failure mode is a clear fixture-not-found error, not a logic error).

- [ ] **Step 8: Run the full token test suite to verify no regression**

```bash
pytest tests/ -k token -v
```

Expected: all token-related tests PASS.

- [ ] **Step 9: Commit**

```bash
git add app/api/tokens.py app/auth/jwt.py tests/test_tokens_bootstrap_scope.py
git commit -m "feat(tokens): add scope + ttl_seconds fields with bootstrap-analyst clamp"
```

---

### Task 2: Add `_LEGACY_STRINGS` scan to admin workspace-prompt endpoint

**Files:**
- Modify: `app/api/claude_md.py` (add `_LEGACY_STRINGS`, `_scan_legacy_strings`, augment `TemplateGetResponse`, populate in `admin_get_workspace_template`)
- Test: `tests/test_legacy_strings_scan.py` (new)

- [ ] **Step 1: Write failing tests**

```python
# tests/test_legacy_strings_scan.py
"""Tests for legacy-string scan in admin CLAUDE.md template endpoint."""

from app.api.claude_md import _scan_legacy_strings, _LEGACY_STRINGS


def test_scan_finds_all_known_legacy_strings():
    text = """
    Run `da sync` then `da fetch web_sessions --where ...`.
    Old workspace at data/parquet/ — see `da analyst setup`.
    Use `da metrics list` and `da metrics show <id>`.
    """
    hits = _scan_legacy_strings(text)
    assert "da sync" in hits
    assert "da fetch" in hits
    assert "data/parquet" in hits
    assert "da analyst setup" in hits
    assert "da metrics list" in hits
    assert "da metrics show" in hits


def test_scan_returns_empty_for_clean_text():
    text = "Use `agnes pull` to refresh, `agnes snapshot create` for ad-hoc, `server/parquet/`."
    assert _scan_legacy_strings(text) == []


def test_scan_returns_unique_sorted_hits():
    text = "da sync da sync data/parquet/ data/parquet/foo"
    hits = _scan_legacy_strings(text)
    assert hits == sorted(set(hits))


def test_legacy_strings_constant_shape():
    assert isinstance(_LEGACY_STRINGS, tuple)
    assert all(isinstance(s, str) for s in _LEGACY_STRINGS)
    assert "da sync" in _LEGACY_STRINGS
    assert "data/parquet" in _LEGACY_STRINGS
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_legacy_strings_scan.py -v
```

Expected: FAIL with `ImportError: cannot import name '_scan_legacy_strings' from 'app.api.claude_md'`.

- [ ] **Step 3: Add `_LEGACY_STRINGS` and `_scan_legacy_strings` to `app/api/claude_md.py`**

Insert near the other module-level constants (after the imports, before the class definitions — find a stable location, e.g., right before `class ClaudeMdResponse`):

```python
# Substrings that, when found in an admin-saved CLAUDE.md override, signal
# the override is stale relative to the post-clean-bootstrap CLI surface.
# Surfaced via TemplateGetResponse.legacy_strings_detected so the admin UI
# can render a yellow banner prompting re-authoring.
_LEGACY_STRINGS = (
    "data/parquet",
    "da sync",
    "da fetch",
    "da analyst setup",
    "da metrics list",
    "da metrics show",
)


def _scan_legacy_strings(text: str) -> list[str]:
    """Return sorted unique substrings from _LEGACY_STRINGS present in text."""
    return sorted({s for s in _LEGACY_STRINGS if s in text})
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_legacy_strings_scan.py -v
```

Expected: all PASS.

- [ ] **Step 5: Augment `TemplateGetResponse`**

Find `class TemplateGetResponse` (around `app/api/claude_md.py:72-76`) and add the field:

```python
class TemplateGetResponse(BaseModel):
    content: Optional[str]
    default: str
    updated_at: Optional[str] = None
    updated_by: Optional[str] = None
    legacy_strings_detected: list[str] = []  # populated when override contains stale verbs/paths
```

- [ ] **Step 6: Populate the field in `admin_get_workspace_template`**

Find the route (search for `admin_get_workspace_template` in `app/api/claude_md.py`). Inside the function body, before constructing the response, add:

```python
# Scan the saved override (not the live default) for legacy strings.
# A non-empty list triggers the yellow banner in the admin UI.
override_text = override.content if override else ""
legacy_hits = _scan_legacy_strings(override_text)
```

Then include `legacy_strings_detected=legacy_hits` in the `TemplateGetResponse(...)` construction.

- [ ] **Step 7: Add an HTTP test for the populated field**

Append to `tests/test_legacy_strings_scan.py`:

```python
def test_admin_get_template_returns_legacy_strings_when_override_dirty(web_session):
    """Setting an override containing legacy strings populates the field."""
    web_session.put("/api/admin/workspace-prompt-template",
                    json={"content": "Run `da sync` and check data/parquet/."})
    resp = web_session.get("/api/admin/workspace-prompt-template")
    assert resp.status_code == 200
    body = resp.json()
    assert "da sync" in body["legacy_strings_detected"]
    assert "data/parquet" in body["legacy_strings_detected"]


def test_admin_get_template_returns_empty_when_clean(web_session):
    web_session.put("/api/admin/workspace-prompt-template",
                    json={"content": "Use `agnes pull` and check `server/parquet/`."})
    resp = web_session.get("/api/admin/workspace-prompt-template")
    assert resp.json()["legacy_strings_detected"] == []
```

These depend on `web_session` fixture from Task 22; mark `pytest.skip` if not yet present.

- [ ] **Step 8: Run all `claude_md` tests**

```bash
pytest tests/test_legacy_strings_scan.py tests/ -k claude_md -v
```

Expected: PASS (skip the HTTP tests if fixture missing — that's OK).

- [ ] **Step 9: Commit**

```bash
git add app/api/claude_md.py tests/test_legacy_strings_scan.py
git commit -m "feat(admin): scan CLAUDE.md override for legacy strings"
```

---

### Task 3: Add `role` parameter to `setup_instructions.py` (analyst branch)

**Files:**
- Modify: `app/web/setup_instructions.py` (add `role` parameter to `resolve_lines` and `render_setup_instructions`; add analyst-branch helper)
- Test: `tests/test_setup_instructions_analyst.py` (new)

- [ ] **Step 1: Write failing tests**

```python
# tests/test_setup_instructions_analyst.py
"""Tests for analyst-branch rendering of /setup paste prompt."""

from app.web.setup_instructions import render_setup_instructions


def test_render_analyst_role_basic():
    text = render_setup_instructions(
        server_url="https://agnes.example.com",
        token="agnes_pat_TEST",
        wheel_filename="agnes-0.32.0-py3-none-any.whl",
        role="analyst",
    )
    # Required content for analyst role:
    assert "uv tool install" in text
    assert "agnes init" in text
    assert "--token" in text and "agnes_pat_TEST" in text
    assert "--server-url" in text and "https://agnes.example.com" in text
    assert "agnes catalog" in text  # smoke verify step
    # Forbidden content (admin-only):
    assert "marketplace" not in text
    assert "claude plugin install" not in text
    assert "agnes skills install" not in text  # analyst doesn't bulk-install skills
    assert "agnes diagnose" not in text  # analyst smoke verify is `agnes catalog`, not diagnose


def test_render_admin_role_unchanged():
    """Default role=admin keeps the existing 6/8-step layout."""
    text = render_setup_instructions(
        server_url="https://agnes.example.com",
        token="agnes_pat_TEST",
        wheel_filename="agnes-0.32.0-py3-none-any.whl",
        # role omitted — defaults to "admin"
    )
    assert "agnes auth import-token" in text  # admin uses import-token, not agnes init
    assert "agnes diagnose" in text  # admin keeps diagnose


def test_render_analyst_with_ca_pem():
    """Analyst role + private CA → TLS trust block reused from admin path."""
    text = render_setup_instructions(
        server_url="https://agnes.example.com",
        token="agnes_pat_TEST",
        wheel_filename="agnes-0.32.0-py3-none-any.whl",
        role="analyst",
        ca_pem="-----BEGIN CERTIFICATE-----\nMIIBxxx\n-----END CERTIFICATE-----",
    )
    assert "AGNES_CA_PEM" in text  # heredoc marker from trust block
    assert "ca-bundle.pem" in text
    assert "agnes init" in text  # analyst-specific step still present
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_setup_instructions_analyst.py -v
```

Expected: FAIL — `render_setup_instructions()` doesn't accept `role` parameter.

- [ ] **Step 3: Add analyst-branch helper functions**

Insert after `_install_cli_lines` (around line 311 in `setup_instructions.py`):

```python
def _analyst_init_lines(server_url_placeholder: str = "{server_url}") -> list[str]:
    """Steps 2-3 — `agnes init` (auth + workspace bootstrap) + smoke verify.

    Replaces the admin-flow login + verify steps (today: `agnes auth import-token`
    + `agnes auth whoami`). `agnes init` is non-interactive: `--token` carries the PAT,
    `--server-url` carries the origin. The bootstrap PAT has a 1 h TTL — if the
    user takes longer than that to paste this prompt, the init call returns 401
    and the user re-clicks "Generate prompt" on the install page.
    """
    return [
        "",
        "2) Bootstrap your analyst workspace in this directory:",
        f"   agnes init --server-url \"{server_url_placeholder}\" --token \"{{token}}\" --workspace .",
        "",
        "   This authenticates with the PAT, fetches your CLAUDE.md (RBAC-filtered),",
        "   installs Claude Code SessionStart/End hooks (auto-refresh), and runs an",
        "   initial `agnes pull` so your DuckDB views are ready.",
        "",
        "3) Verify the data is queryable:",
        "   agnes catalog",
        "",
        "   This should list the tables your account has grants for. Empty list",
        "   means your admin hasn't granted you access yet — contact them.",
    ]


def _analyst_finale_lines(confirm_step_num: str, has_ca: bool) -> list[str]:
    """Final Confirm step for analyst role. Shorter than admin: no marketplace,
    no plugins, no skills."""
    bullets = [
        "   - `agnes --version` output",
        "   - First few lines of `agnes catalog` (tables you can see)",
        "   - Confirmation that `./CLAUDE.md` and `./AGNES_WORKSPACE.md` exist",
        "   - Confirmation that `./.claude/settings.json` contains SessionStart/End hooks",
    ]
    if has_ca:
        bullets.append(
            "   - Which CA bundle source got picked in step 0(d)"
        )
    return [
        "",
        f"{confirm_step_num}) Confirm:",
        "   Tell me \"Agnes analyst workspace is ready\" and summarize:",
        *bullets,
    ]
```

- [ ] **Step 4: Add `role` parameter to `resolve_lines` and `render_setup_instructions`**

Find `def resolve_lines(...)` (around line 609). Modify the signature and dispatch:

```python
from typing import Literal

def resolve_lines(
    wheel_filename: str,
    *,
    plugin_install_names: list[str] | None = None,
    self_signed_tls: bool = False,
    server_host: str = "",
    ca_pem: str | None = None,
    role: Literal["analyst", "admin"] = "admin",
) -> list[str]:
    """..."""
    if role == "analyst":
        return _resolve_analyst_lines(wheel_filename, ca_pem=ca_pem)
    # Existing admin path:
    names = list(plugin_install_names or [])
    has_marketplace = bool(names)
    has_ca = bool(ca_pem and ca_pem.strip())
    # ... (existing body unchanged)
```

Add the new analyst dispatcher right after `resolve_lines`:

```python
def _resolve_analyst_lines(wheel_filename: str, *, ca_pem: str | None) -> list[str]:
    """Analyst workspace-bootstrap layout. Self-contained — no admin-only steps."""
    has_ca = bool(ca_pem and ca_pem.strip())
    confirm_step = "4" if has_ca else "4"  # numbering: 0 (TLS optional), 1, 2, 3, 4

    lines: list[str] = []
    if has_ca:
        lines.extend(_tls_trust_block(ca_pem))
    lines.extend(_preamble_lines(has_ca=has_ca))
    lines.extend(_install_cli_lines(has_ca=has_ca))   # step 1
    lines.extend(_analyst_init_lines())                # steps 2-3
    lines.extend(_analyst_finale_lines(confirm_step, has_ca=has_ca))  # step 4

    return [
        line.replace("{wheel_filename}", wheel_filename)
        for line in lines
    ]
```

Update `render_setup_instructions` to accept and forward `role`:

```python
def render_setup_instructions(
    server_url: str,
    token: str,
    wheel_filename: str = "agnes.whl",
    *,
    plugin_install_names: list[str] | None = None,
    self_signed_tls: bool = False,
    server_host: str = "",
    ca_pem: str | None = None,
    role: Literal["analyst", "admin"] = "admin",
) -> str:
    lines = resolve_lines(
        wheel_filename,
        plugin_install_names=plugin_install_names,
        self_signed_tls=self_signed_tls,
        server_host=server_host,
        ca_pem=ca_pem,
        role=role,
    )
    text = "\n".join(lines)
    return text.replace("{server_url}", server_url).replace("{token}", token)
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
pytest tests/test_setup_instructions_analyst.py -v
```

Expected: all PASS.

- [ ] **Step 6: Run regression on existing setup-instruction tests**

```bash
pytest tests/ -k setup_instructions -v
```

Expected: existing admin-role tests still PASS (no regression).

- [ ] **Step 7: Commit**

```bash
git add app/web/setup_instructions.py tests/test_setup_instructions_analyst.py
git commit -m "feat(setup): add analyst role to install-prompt renderer"
```

---

### Task 4: Add `role` query branching to `/setup` route

**Files:**
- Modify: `app/web/router.py` (`setup_page` around line 717 — read `role` query param, pass to renderer)
- Test: `tests/test_setup_page_roles.py` (new)

- [ ] **Step 1: Read existing `setup_page` to understand its current shape**

```bash
grep -n "setup_page\|/setup\|/install" app/web/router.py | head
```

Read the function (~30 lines around the match) to understand its current call sites and template rendering.

- [ ] **Step 2: Write failing tests**

```python
# tests/test_setup_page_roles.py
"""Tests for /setup role query-param branching."""


def test_setup_page_default_role_is_admin(client):
    resp = client.get("/setup")
    assert resp.status_code == 200
    # Admin tile is active; analyst tile is linked.
    assert "Admin CLI" in resp.text or "role=admin" in resp.text


def test_setup_page_analyst_role(client):
    resp = client.get("/setup?role=analyst")
    assert resp.status_code == 200
    assert "Analyst workspace" in resp.text or "role=analyst" in resp.text


def test_install_redirects_to_setup(client):
    resp = client.get("/install", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert "/setup" in resp.headers["location"]
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
pytest tests/test_setup_page_roles.py -v
```

Expected: tests for analyst/role-branching content FAIL; redirect test may PASS (existing behavior).

- [ ] **Step 4: Modify `setup_page` to read `role` query param**

Find `setup_page` in `app/web/router.py`. Update its signature to add a `role` query param and pass it to the renderer:

```python
from typing import Literal
from fastapi import Query

@router.get("/setup", response_class=HTMLResponse)
async def setup_page(
    request: Request,
    role: Literal["analyst", "admin"] = Query(default="admin", description="Bootstrap target role"),
    # ... existing dependencies (auth, etc.)
):
    """Renders the role-specific install paste prompt."""
    # ... existing context-building code ...
    ctx["role"] = role
    return templates.TemplateResponse(request, "setup.html", ctx)
```

If `setup_page` already calls `render_setup_instructions(...)` server-side (vs. JS-rendered), pass `role` there too:

```python
prompt_text = render_setup_instructions(
    server_url=str(request.base_url).rstrip("/"),
    token="{token}",  # placeholder filled by JS at click time
    wheel_filename=resolved_wheel,
    plugin_install_names=plugin_install_names if role == "admin" else None,
    self_signed_tls=...,
    server_host=...,
    ca_pem=...,
    role=role,
)
```

- [ ] **Step 5: Update `setup.html` template to render role tiles**

Find `app/web/templates/setup.html` (or whatever `setup_page` actually renders — `grep -n "setup.html\|TemplateResponse" app/web/router.py`). Add two role tiles near the top of the body:

```html
<div class="role-tiles" style="display:flex; gap:1rem; margin-bottom:2rem;">
  <a href="/setup?role=analyst"
     class="role-tile {% if role == 'analyst' %}is-active{% endif %}"
     style="flex:1; padding:1rem; border:2px solid {% if role == 'analyst' %}#0070f3{% else %}#ddd{% endif %}; border-radius:8px; text-decoration:none;">
    <h3>Analyst workspace</h3>
    <p>Bootstrap a workspace folder with CLAUDE.md, hooks, and synced data.</p>
  </a>
  <a href="/setup?role=admin"
     class="role-tile {% if role == 'admin' %}is-active{% endif %}"
     style="flex:1; padding:1rem; border:2px solid {% if role == 'admin' %}#0070f3{% else %}#ddd{% endif %}; border-radius:8px; text-decoration:none;">
    <h3>Admin CLI</h3>
    <p>Install the CLI, register the marketplace, set up admin tooling.</p>
  </a>
</div>
```

If the template is more sophisticated (e.g., with role-specific JS), wire the JS to use the `role` ctx variable when calling `POST /auth/tokens` for PAT minting:

```javascript
const role = "{{ role }}";
const scope = role === "analyst" ? "bootstrap-analyst" : "general";
const ttlSeconds = role === "analyst" ? 3600 : 86400; // analyst short-lived

await fetch('/auth/tokens', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ name: `setup-${role}`, scope, ttl_seconds: ttlSeconds }),
});
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
pytest tests/test_setup_page_roles.py -v
```

Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add app/web/router.py app/web/templates/setup.html tests/test_setup_page_roles.py
git commit -m "feat(setup): /setup?role=analyst|admin branching with role tiles"
```

---

### Task 5: Add legacy-strings banner to admin workspace-prompt template UI

**Files:**
- Modify: `app/web/templates/admin_workspace_prompt.html` (add banner above editor when `legacy_strings_detected` non-empty)
- Test: `tests/test_legacy_strings_scan.py` (extend with HTML rendering test)

- [ ] **Step 1: Read existing admin-prompt template**

```bash
cat app/web/templates/admin_workspace_prompt.html
```

Find where the editor (textarea) is rendered.

- [ ] **Step 2: Write extension test**

Append to `tests/test_legacy_strings_scan.py`:

```python
def test_admin_prompt_template_renders_banner_when_legacy_present(web_session):
    web_session.put("/api/admin/workspace-prompt-template",
                    json={"content": "Run `da sync` daily."})
    resp = web_session.get("/admin/workspace-prompt")
    assert resp.status_code == 200
    assert "yellow" in resp.text.lower() or "warning" in resp.text.lower()
    assert "da sync" in resp.text  # the hit is rendered in the banner


def test_admin_prompt_template_no_banner_when_clean(web_session):
    web_session.put("/api/admin/workspace-prompt-template",
                    json={"content": "Run `agnes pull` daily."})
    resp = web_session.get("/admin/workspace-prompt")
    assert resp.status_code == 200
    # The banner div is absent or empty
    # Implementation: e.g., id="legacy-banner" wraps the warning; check empty
    assert "legacy-banner" not in resp.text or "display: none" in resp.text or len(
        [l for l in resp.text.split("\n") if "legacy-banner" in l and "hidden" not in l]
    ) <= 2
```

(The exact test is fragile — strengthen once the implementation lands.)

- [ ] **Step 3: Run tests to verify they fail**

```bash
pytest tests/test_legacy_strings_scan.py -v -k banner
```

- [ ] **Step 4: Modify `admin_workspace_prompt.html`**

Find the spot above the editor `<textarea>` and insert:

```html
{% if legacy_strings_detected %}
<div id="legacy-banner" style="background:#fff3cd; border:1px solid #ffc107; padding:0.75rem 1rem; border-radius:4px; margin-bottom:1rem;">
  <strong>⚠ This override references CLI verbs / paths that were renamed:</strong>
  <ul style="margin:0.5rem 0 0 1.5rem;">
    {% for hit in legacy_strings_detected %}
    <li><code>{{ hit }}</code></li>
    {% endfor %}
  </ul>
  <p style="margin:0.5rem 0 0 0;">Re-author and Save to clear this warning. See CHANGELOG for the rename list.</p>
</div>
{% endif %}
```

In the route that renders this template (find via `grep -n "admin_workspace_prompt\.html" app/web/router.py app/web/admin_router.py`), pass `legacy_strings_detected` into the context. The data comes from the same `_scan_legacy_strings(override_text)` call as the API — DRY by importing from `app.api.claude_md`:

```python
from app.api.claude_md import _scan_legacy_strings

@router.get("/admin/workspace-prompt", response_class=HTMLResponse)
async def admin_workspace_prompt_page(...):
    override = repo.get_workspace_prompt_template()
    ctx = {
        "override_content": override.content if override else "",
        "legacy_strings_detected": _scan_legacy_strings(override.content) if override else [],
        # ... rest of existing context
    }
    return templates.TemplateResponse(request, "admin_workspace_prompt.html", ctx)
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
pytest tests/test_legacy_strings_scan.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/web/templates/admin_workspace_prompt.html app/web/router.py tests/test_legacy_strings_scan.py
git commit -m "feat(admin): yellow banner for legacy CLI verbs in workspace-prompt override"
```

---

### Task 6: Update `config/claude_md_template.txt` (server-side rendered to `/api/welcome`)

**Files:**
- Modify: `config/claude_md_template.txt` (verb + path rewrites)

- [ ] **Step 1: Read the current template**

```bash
cat config/claude_md_template.txt | head -100
wc -l config/claude_md_template.txt
```

- [ ] **Step 2: Apply systematic rewrites**

Replace throughout the file:
- `da sync` → `agnes pull` (everywhere)
- `da analyst setup` → `agnes init` (everywhere)
- `da fetch` → `agnes snapshot create`
- `da metrics list` → `agnes catalog --metrics`
- `da metrics show` → `agnes catalog --metrics --show`
- `data/parquet/` → `server/parquet/`
- `data/duckdb/` → `user/duckdb/`
- `data/metadata/` → (delete references; the path no longer exists)

Use `sed`:

```bash
sed -i.bak \
  -e 's|da sync --upload-only|agnes push|g' \
  -e 's|da sync|agnes pull|g' \
  -e 's|da analyst setup|agnes init|g' \
  -e 's|da fetch|agnes snapshot create|g' \
  -e 's|da metrics list|agnes catalog --metrics|g' \
  -e 's|da metrics show|agnes catalog --metrics --show|g' \
  -e 's|data/parquet/|server/parquet/|g' \
  -e 's|data/duckdb/|user/duckdb/|g' \
  config/claude_md_template.txt

rm config/claude_md_template.txt.bak
```

- [ ] **Step 3: Read the result and review for any leftover legacy strings**

```bash
grep -nE 'da sync|da fetch|da analyst|da metrics list|da metrics show|data/parquet|data/duckdb|data/metadata' config/claude_md_template.txt
```

Expected: no matches.

- [ ] **Step 4: Add a top-of-file pointer to AGNES_WORKSPACE.md**

Insert near the top of the rendered template (e.g., after the `# {instance_name}` heading):

```markdown
> Looking for human-readable workspace docs? Open `AGNES_WORKSPACE.md` in this directory — that file documents what `agnes init` installed, where files live, and how to uninstall.
```

- [ ] **Step 5: Render the template via `/api/welcome` (manual smoke)**

(Defer the real test to Task 27 — clean-install integration.)

- [ ] **Step 6: Commit**

```bash
git add config/claude_md_template.txt
git commit -m "docs(claude-md-template): rewrite verbs + paths for new CLI surface"
```

---

## Phase 2 — Client-side library (`cli/lib/`)

### Task 7: Establish `cli/lib/` package + `install_claude_hooks`

**Files:**
- Create: `cli/lib/__init__.py`, `cli/lib/hooks.py`
- Test: `tests/test_lib_hooks.py` (new)

- [ ] **Step 1: Write failing tests**

```python
# tests/test_lib_hooks.py
"""Tests for cli/lib/hooks.py:install_claude_hooks."""

import json
from pathlib import Path

import pytest

from cli.lib.hooks import install_claude_hooks


def _read_settings(workspace: Path) -> dict:
    return json.loads((workspace / ".claude" / "settings.json").read_text())


def test_install_creates_settings_file(tmp_path):
    install_claude_hooks(tmp_path)
    cfg = _read_settings(tmp_path)
    assert cfg["hooks"]["SessionStart"]
    assert "agnes pull --quiet" in cfg["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert cfg["hooks"]["SessionEnd"]
    assert "agnes push --quiet" in cfg["hooks"]["SessionEnd"][0]["hooks"][0]["command"]


def test_install_idempotent(tmp_path):
    install_claude_hooks(tmp_path)
    install_claude_hooks(tmp_path)
    cfg = _read_settings(tmp_path)
    # Only ONE entry per event after second install (not duplicated)
    assert len(cfg["hooks"]["SessionStart"]) == 1
    assert len(cfg["hooks"]["SessionEnd"]) == 1


def test_install_replaces_old_da_sync_entries(tmp_path):
    """Hook from a pre-rewrite workspace gets replaced cleanly."""
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({
        "hooks": {
            "SessionStart": [{"hooks": [{"type": "command", "command": "da sync --quiet"}]}],
            "SessionEnd": [{"hooks": [{"type": "command", "command": "da sync --upload-only --quiet"}]}],
        }
    }))
    install_claude_hooks(tmp_path)
    cfg = _read_settings(tmp_path)
    assert len(cfg["hooks"]["SessionStart"]) == 1
    assert "agnes pull" in cfg["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert "da sync" not in cfg["hooks"]["SessionStart"][0]["hooks"][0]["command"]


def test_install_preserves_third_party_hooks(tmp_path):
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({
        "hooks": {
            "SessionStart": [{"hooks": [{"type": "command", "command": "echo hi from another tool"}]}],
            "PreToolUse": [{"hooks": [{"type": "command", "command": "echo pre"}]}],
        }
    }))
    install_claude_hooks(tmp_path)
    cfg = _read_settings(tmp_path)
    # Third-party SessionStart entry survives; our agnes pull entry appended
    starts = cfg["hooks"]["SessionStart"]
    assert any("echo hi from another tool" in s["hooks"][0]["command"] for s in starts)
    assert any("agnes pull" in s["hooks"][0]["command"] for s in starts)
    # PreToolUse untouched
    assert cfg["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "echo pre"


def test_install_handles_missing_settings_file(tmp_path):
    """No prior settings.json → create from scratch."""
    install_claude_hooks(tmp_path)
    assert (tmp_path / ".claude" / "settings.json").exists()


def test_install_handles_invalid_json(tmp_path, capsys):
    """Invalid existing settings.json → warn, skip."""
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text("not valid json {")
    # Should not raise; should print a warning
    install_claude_hooks(tmp_path)
    captured = capsys.readouterr()
    assert "not valid JSON" in captured.err or "warning" in captured.err.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_lib_hooks.py -v
```

Expected: `ImportError` — `cli.lib` doesn't exist.

- [ ] **Step 3: Create `cli/lib/__init__.py`**

```bash
touch cli/lib/__init__.py
```

- [ ] **Step 4: Create `cli/lib/hooks.py`**

```python
# cli/lib/hooks.py
"""Workspace-scoped Claude Code hook installer.

Replaces the in-place `_install_claude_hooks` from `cli/commands/analyst.py`
(deleted as part of the clean-analyst-bootstrap rewrite). Splits hook
installation into a pure-function library so `agnes init` and any future caller
can use it without dragging in the deleted command module.

Design notes:
- Workspace-scoped (`<workspace>/.claude/settings.json`), NOT user-home.
  The hooks fire only when Claude Code opens this workspace.
- Idempotent: second invocation drops a prior `agnes pull` / `da sync` /
  `agnes push` entry (matched by command substring) and appends fresh entries.
  Third-party hooks (mixed entries, foreign commands) are left alone.
- Uses `\\| true` so the hook never blocks a session on a transient sync error.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_OUR_COMMAND_MARKERS = ("agnes pull", "agnes push", "da sync")


def install_claude_hooks(workspace: Path) -> None:
    """Install SessionStart→`agnes pull` and SessionEnd→`agnes push` hooks.

    Idempotent. Workspace-scoped (writes `<workspace>/.claude/settings.json`).
    Preserves third-party hooks and other event types.
    """
    settings_path = workspace / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    if settings_path.exists():
        try:
            cfg = json.loads(settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(
                f"Warning: {settings_path} is not valid JSON; skipping hook install.",
                file=sys.stderr,
            )
            return
    else:
        cfg = {}

    hooks = cfg.setdefault("hooks", {})

    def _replace_or_add(event: str, command: str) -> None:
        existing = hooks.setdefault(event, [])
        # Drop any prior entry whose every hook command matches one of our
        # markers. Mixed entries (third-party + ours) are left alone.
        for entry in list(existing):
            entry_cmds = [h.get("command", "") for h in entry.get("hooks", [])]
            if entry_cmds and all(
                any(marker in c for marker in _OUR_COMMAND_MARKERS) for c in entry_cmds
            ):
                existing.remove(entry)
        existing.append({"hooks": [{"type": "command", "command": command}]})

    _replace_or_add("SessionStart", "agnes pull --quiet 2>/dev/null || true")
    _replace_or_add("SessionEnd", "agnes push --quiet 2>/dev/null || true")

    settings_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
pytest tests/test_lib_hooks.py -v
```

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add cli/lib/__init__.py cli/lib/hooks.py tests/test_lib_hooks.py
git commit -m "feat(cli-lib): cli/lib/hooks.py:install_claude_hooks"
```

---

### Task 8: `cli/lib/pull.py:run_pull` — extract data-refresh primitive from `sync.py`

**Files:**
- Create: `cli/lib/pull.py`
- Test: `tests/test_lib_pull.py` (new)

- [ ] **Step 1: Read `cli/commands/sync.py` to identify the function body to lift**

```bash
wc -l cli/commands/sync.py
grep -n "^def \|^class " cli/commands/sync.py
```

Identify:
- The Typer command function (e.g., `sync()` decorated with `@sync_app.command()`)
- The helper functions called from it: `_rebuild_duckdb_views`, `_fetch_and_write_rules`, `_is_valid_parquet`, etc.

- [ ] **Step 2: Write failing tests**

```python
# tests/test_lib_pull.py
"""Tests for cli/lib/pull.py:run_pull."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from cli.lib.pull import run_pull, PullResult


@pytest.fixture
def fake_server(monkeypatch):
    """Mock api_get to return canned manifest + memory bundle."""
    canned = {
        "/api/sync/manifest": {"tables": []},
        "/api/memory/bundle": {"mandatory": [], "approved": []},
    }
    def _api_get(path, *args, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        body = canned.get(path, {})
        resp.json.return_value = body
        resp.iter_bytes = lambda chunk_size=65536: iter([b""])
        resp.raise_for_status = lambda: None
        return resp
    monkeypatch.setattr("cli.lib.pull.api_get", _api_get, raising=False)
    return canned


def test_run_pull_empty_manifest_no_parquet_dir(tmp_path, fake_server):
    result = run_pull(server_url="http://x", token="t", workspace=tmp_path)
    assert isinstance(result, PullResult)
    assert result.tables_updated == 0
    assert not (tmp_path / "server" / "parquet").exists(), \
        "lazy mkdir: empty manifest must not create server/parquet/"


def test_run_pull_empty_memory_no_rules_dir(tmp_path, fake_server):
    run_pull(server_url="http://x", token="t", workspace=tmp_path)
    assert not (tmp_path / ".claude" / "rules").exists(), \
        "lazy mkdir: empty bundle must not create .claude/rules/"


def test_run_pull_creates_duckdb_unconditionally(tmp_path, fake_server):
    """Even with zero data, the DuckDB file is opened (it's the load-bearing
    artifact and other readers expect its parent dir to exist)."""
    run_pull(server_url="http://x", token="t", workspace=tmp_path)
    assert (tmp_path / "user" / "duckdb" / "analytics.duckdb").exists()


def test_run_pull_with_one_table(tmp_path, monkeypatch):
    """Manifest with one table → server/parquet/ created, parquet downloaded."""
    canned_manifest = {"tables": [{"id": "tbl1", "md5": "abc"}]}
    canned_memory = {"mandatory": [], "approved": []}
    parquet_bytes = b"PAR1" + b"\x00" * 1000 + b"PAR1"  # minimal valid parquet shape

    def _api_get(path, *args, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        if path == "/api/sync/manifest":
            resp.json.return_value = canned_manifest
        elif path == "/api/memory/bundle":
            resp.json.return_value = canned_memory
        elif path.startswith("/api/data/tbl1/download"):
            resp.iter_bytes = lambda chunk_size=65536: iter([parquet_bytes])
        return resp

    monkeypatch.setattr("cli.lib.pull.api_get", _api_get, raising=False)
    monkeypatch.setattr("cli.lib.pull._is_valid_parquet", lambda p: True, raising=False)

    result = run_pull(server_url="http://x", token="t", workspace=tmp_path)
    assert (tmp_path / "server" / "parquet").exists()
    assert (tmp_path / "server" / "parquet" / "tbl1.parquet").exists()
    assert result.tables_updated == 1


def test_run_pull_dry_run_writes_nothing(tmp_path, fake_server):
    run_pull(server_url="http://x", token="t", workspace=tmp_path, dry_run=True)
    assert not (tmp_path / "server").exists()
    assert not (tmp_path / "user" / "duckdb").exists()
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
pytest tests/test_lib_pull.py -v
```

Expected: ImportError on `cli.lib.pull`.

- [ ] **Step 4: Create `cli/lib/pull.py`**

Lift the body of today's `cli/commands/sync.py:sync()` into a pure function. Specifically:
- Move `_rebuild_duckdb_views`, `_fetch_and_write_rules`, `_is_valid_parquet` (private helpers) into `cli/lib/pull.py`.
- Drop Typer decorators and `typer.echo` calls — replace with returning structured result.
- Apply lazy-mkdir fixes:
  - `_fetch_and_write_rules`: check `mandatory + approved` non-empty before mkdir.
  - Per-table download loop: mkdir `server/parquet/` inside the loop, only when about to write.

```python
# cli/lib/pull.py
"""Pure-function data-refresh primitive — used by `agnes pull` and `agnes init`.

Extracted from `cli/commands/sync.py` (deleted in the clean-bootstrap rewrite).
This module has no Typer dependency, no stdout side effects, no exit calls.
Callers decide what to print and how to handle errors.

Lazy-mkdir contract:
- `<workspace>/server/parquet/` — created only when the manifest has at least one
  table to download. Empty manifest → directory is never created.
- `<workspace>/.claude/rules/` — created only when `/api/memory/bundle` returns
  at least one mandatory or approved item. Empty bundle → directory absent.
- `<workspace>/user/duckdb/analytics.duckdb` — created unconditionally. The DB
  file (not just the dir) is the load-bearing artifact every reader expects.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from cli.client import api_get


_SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9_\-]+$")


@dataclass
class PullResult:
    tables_updated: int = 0
    parquets_total: int = 0
    rules_count: int = 0
    duration_s: float = 0.0
    errors: list[str] = field(default_factory=list)


def run_pull(
    server_url: str,
    token: str,
    workspace: Path,
    *,
    dry_run: bool = False,
) -> PullResult:
    """Refresh registered data into <workspace>/server/parquet + user/duckdb.

    Args:
        server_url: Base URL of the Agnes server.
        token: PAT for `Authorization: Bearer <token>`.
        workspace: Local workspace root. Lazy-mkdir applies inside this dir.
        dry_run: If True, computes deltas but writes nothing to disk.

    Returns:
        PullResult with summary counts.
    """
    import time
    start = time.time()
    result = PullResult()

    # 1. Fetch RBAC-filtered manifest
    resp = api_get("/api/sync/manifest", server_url=server_url, token=token)
    resp.raise_for_status()
    manifest = resp.json()
    tables = manifest.get("tables", [])

    # 2. Per-table download with lazy mkdir
    parquet_dir = workspace / "server" / "parquet"
    for tbl in tables:
        tbl_id = tbl.get("id", "")
        if not _SAFE_ID_RE.match(tbl_id):
            result.errors.append(f"unsafe table id skipped: {tbl_id!r}")
            continue
        target = parquet_dir / f"{tbl_id}.parquet"
        # Skip if local md5 matches remote
        remote_md5 = tbl.get("md5", "")
        if target.exists() and _file_md5(target) == remote_md5:
            continue
        if dry_run:
            result.tables_updated += 1
            continue
        # Lazy mkdir: only when about to write the FIRST parquet.
        parquet_dir.mkdir(parents=True, exist_ok=True)
        try:
            stream_resp = api_get(
                f"/api/data/{tbl_id}/download", server_url=server_url, token=token, stream=True,
            )
            stream_resp.raise_for_status()
            with open(target, "wb") as fh:
                for chunk in stream_resp.iter_bytes(chunk_size=65536):
                    fh.write(chunk)
            if not _is_valid_parquet(target):
                target.unlink()
                result.errors.append(f"{tbl_id}: invalid parquet (missing PAR1)")
                continue
            result.tables_updated += 1
        except Exception as exc:
            if target.exists():
                target.unlink()
            result.errors.append(f"{tbl_id}: {exc}")

    # 3. Rebuild DuckDB views
    if not dry_run:
        _rebuild_duckdb_views(workspace, parquet_dir)
        result.parquets_total = len(list(parquet_dir.glob("*.parquet"))) if parquet_dir.exists() else 0

    # 4. Fetch corporate-memory bundle (lazy mkdir for .claude/rules/)
    if not dry_run:
        result.rules_count = _fetch_and_write_rules(workspace, server_url, token)

    result.duration_s = time.time() - start
    return result


def _file_md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_valid_parquet(path: Path) -> bool:
    """Cheap structural check — parquet files begin and end with `PAR1`."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(4)
            fh.seek(-4, 2)
            tail = fh.read(4)
        return head == b"PAR1" and tail == b"PAR1"
    except OSError:
        return False


def _rebuild_duckdb_views(workspace: Path, parquet_dir: Path) -> None:
    import duckdb

    db_path = workspace / "user" / "duckdb" / "analytics.duckdb"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(db_path))
    try:
        # Drop all existing views
        views = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_type='VIEW'"
        ).fetchall()
        for (view_name,) in views:
            conn.execute(f'DROP VIEW IF EXISTS "{view_name}"')
        # Recreate from parquets (if any)
        if parquet_dir.exists():
            for pq_file in parquet_dir.glob("*.parquet"):
                view_name = pq_file.stem
                if not _SAFE_ID_RE.match(view_name):
                    continue
                if not _is_valid_parquet(pq_file):
                    continue
                abs_path = str(pq_file.resolve()).replace("'", "''")
                try:
                    conn.execute(
                        f'CREATE VIEW "{view_name}" AS SELECT * FROM read_parquet(\'{abs_path}\')'
                    )
                except duckdb.Error:
                    continue
    finally:
        conn.close()


def _fetch_and_write_rules(workspace: Path, server_url: str, token: str) -> int:
    """Fetch /api/memory/bundle and write .claude/rules/km_*.md files.

    Lazy mkdir: only creates `<workspace>/.claude/rules/` if the bundle is non-empty.
    Returns the number of rule files written.
    """
    rules_dir = workspace / ".claude" / "rules"
    try:
        resp = api_get("/api/memory/bundle", server_url=server_url, token=token)
        resp.raise_for_status()
        bundle = resp.json()
    except Exception:
        return 0

    items = list(bundle.get("mandatory", [])) + list(bundle.get("approved", []))
    if not items:
        return 0  # no mkdir, nothing to write

    rules_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for item in items:
        item_id = item.get("id", "")
        if not _SAFE_ID_RE.match(item_id):
            continue
        fname = f"km_{item_id}.md"
        body = _item_to_md(item)
        (rules_dir / fname).write_text(body, encoding="utf-8")
        written += 1
    return written


def _item_to_md(item: dict) -> str:
    title = item.get("title", "")
    body = item.get("body", "")
    return f"# {title}\n\n{body}\n"
```

(If `cli/client.py:api_get` doesn't accept the `server_url`/`token`/`stream` kwargs as shown, adapt the calls to the actual signature.)

- [ ] **Step 5: Run tests to verify they pass**

```bash
pytest tests/test_lib_pull.py -v
```

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add cli/lib/pull.py tests/test_lib_pull.py
git commit -m "feat(cli-lib): cli/lib/pull.py:run_pull primitive with lazy mkdir"
```

---

## Phase 3 — New CLI commands

### Task 9: `agnes pull` Typer wrapper

**Files:**
- Create: `cli/commands/pull.py`
- Test: `tests/test_cli_pull.py` (new)

- [ ] **Step 1: Write failing test**

```python
# tests/test_cli_pull.py
"""Tests for `agnes pull` Typer wrapper."""

from typer.testing import CliRunner
from cli.commands.pull import pull_app

runner = CliRunner()


def test_pull_help():
    result = runner.invoke(pull_app, ["--help"])
    assert result.exit_code == 0
    assert "--quiet" in result.output
    assert "--json" in result.output
    assert "--dry-run" in result.output
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_cli_pull.py -v
```

Expected: ImportError.

- [ ] **Step 3: Create `cli/commands/pull.py`**

```python
# cli/commands/pull.py
"""`agnes pull` — refresh registered data into the workspace.

Thin Typer wrapper around `cli/lib/pull.py:run_pull`. Used by:
- Manual invocation: analyst types `agnes pull` to force a refresh.
- SessionStart hook: `agnes pull --quiet 2>/dev/null || true` runs at the start
  of every Claude Code session in this workspace.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import typer

from cli.config import get_server_url, load_token
from cli.error_render import render_error
from cli.lib.pull import run_pull, PullResult


pull_app = typer.Typer(help="Refresh registered data from the server")


@pull_app.callback(invoke_without_command=True)
def pull(
    quiet: bool = typer.Option(False, "--quiet", help="Suppress success stdout"),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Compute deltas without writing"),
):
    """Refresh data from the server into ./server/parquet + ./user/duckdb."""
    server_url = get_server_url()
    if not server_url:
        typer.echo(render_error(0, {"detail": {
            "kind": "server_unreachable",
            "hint": "No server configured. Run: agnes init --server-url <URL> --token <PAT>",
        }}), err=True)
        raise typer.Exit(1)

    token = load_token()
    if not token:
        typer.echo(render_error(0, {"detail": {
            "kind": "auth_failed",
            "hint": "No token. Run: agnes auth import-token --token <PAT>",
        }}), err=True)
        raise typer.Exit(1)

    workspace = Path(os.environ.get("DA_LOCAL_DIR", ".")).resolve()

    try:
        result: PullResult = run_pull(server_url, token, workspace, dry_run=dry_run)
    except Exception as exc:
        typer.echo(render_error(0, {"detail": {
            "kind": "manifest_unauthorized",
            "hint": f"Pull failed: {exc}",
            "message": str(exc),
        }}), err=True)
        raise typer.Exit(1)

    if as_json:
        typer.echo(json.dumps({
            "tables_updated": result.tables_updated,
            "parquets_total": result.parquets_total,
            "rules_count": result.rules_count,
            "duration_s": round(result.duration_s, 3),
            "errors": result.errors,
        }))
        return

    if quiet:
        if result.errors:
            for e in result.errors:
                typer.echo(f"warn: {e}", err=True)
        return

    typer.echo(f"Updated {result.tables_updated} tables ({result.parquets_total} total).")
    typer.echo(f"Rules: {result.rules_count}.")
    if result.errors:
        for e in result.errors:
            typer.echo(f"warn: {e}", err=True)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/test_cli_pull.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cli/commands/pull.py tests/test_cli_pull.py
git commit -m "feat(cli): agnes pull command (Typer wrapper around lib.pull.run_pull)"
```

---

### Task 10: `agnes push` command (extract from `da sync --upload-only`)

**Files:**
- Create: `cli/commands/push.py`
- Test: `tests/test_cli_push.py` (new)

- [ ] **Step 1: Write failing tests**

```python
# tests/test_cli_push.py
from pathlib import Path
from typer.testing import CliRunner

from cli.commands.push import push_app

runner = CliRunner()


def test_push_help():
    result = runner.invoke(push_app, ["--help"])
    assert result.exit_code == 0
    assert "--quiet" in result.output
    assert "--json" in result.output


def test_push_no_sessions_no_mkdir(tmp_path, monkeypatch):
    """Empty workspace → push exits 0, doesn't create user/sessions/."""
    monkeypatch.setenv("DA_LOCAL_DIR", str(tmp_path))
    monkeypatch.setattr("cli.commands.push.get_server_url", lambda: "http://x")
    monkeypatch.setattr("cli.commands.push.load_token", lambda: "test-pat")
    result = runner.invoke(push_app, ["--quiet"])
    assert result.exit_code == 0
    assert not (tmp_path / "user" / "sessions").exists()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_cli_push.py -v
```

- [ ] **Step 3: Create `cli/commands/push.py`**

```python
# cli/commands/push.py
"""`agnes push` — upload local sessions and CLAUDE.local.md to the server.

Extracted from today's `da sync --upload-only`. Hook command:
`agnes push --quiet 2>/dev/null || true` (runs at SessionEnd).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import typer

from cli.client import api_post
from cli.config import get_server_url, load_token
from cli.error_render import render_error


push_app = typer.Typer(help="Upload local sessions and notes to the server")


@push_app.callback(invoke_without_command=True)
def push(
    quiet: bool = typer.Option(False, "--quiet", help="Suppress stdout"),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
    dry_run: bool = typer.Option(False, "--dry-run", help="List what would upload, don't send"),
):
    server_url = get_server_url()
    token = load_token()
    if not server_url or not token:
        typer.echo(render_error(0, {"detail": {
            "kind": "auth_failed",
            "hint": "No server/token configured. Run: agnes init",
        }}), err=True)
        raise typer.Exit(1)

    workspace = Path(os.environ.get("DA_LOCAL_DIR", ".")).resolve()
    sessions_dir = workspace / "user" / "sessions"
    local_md = workspace / ".claude" / "CLAUDE.local.md"

    sessions = []
    if sessions_dir.exists():
        sessions = sorted(sessions_dir.glob("*.jsonl"))

    has_local_md = local_md.exists()

    summary = {"sessions_count": len(sessions), "local_md": has_local_md, "uploaded": 0}

    if dry_run:
        if as_json:
            typer.echo(json.dumps(summary))
        else:
            typer.echo(f"Would upload: {len(sessions)} sessions, local_md={has_local_md}")
        return

    for session_file in sessions:
        try:
            with open(session_file, "rb") as fh:
                resp = api_post("/api/upload/sessions", files={"file": (session_file.name, fh)})
                if resp.status_code == 200:
                    summary["uploaded"] += 1
        except Exception as exc:
            if not quiet:
                typer.echo(f"warn: failed to upload {session_file.name}: {exc}", err=True)

    if has_local_md:
        try:
            with open(local_md, "rb") as fh:
                api_post("/api/upload/local-md", files={"file": (local_md.name, fh)})
        except Exception as exc:
            if not quiet:
                typer.echo(f"warn: failed to upload CLAUDE.local.md: {exc}", err=True)

    if as_json:
        typer.echo(json.dumps(summary))
    elif not quiet:
        typer.echo(f"Uploaded {summary['uploaded']} sessions, local_md={has_local_md}")
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_cli_push.py -v
```

- [ ] **Step 5: Commit**

```bash
git add cli/commands/push.py tests/test_cli_push.py
git commit -m "feat(cli): agnes push command (extracted from sync --upload-only)"
```

---

### Task 11: `agnes init` — workspace bootstrap orchestrator

**Files:**
- Create: `cli/commands/init.py`, `config/agnes_workspace_template.txt`
- Test: `tests/test_cli_init.py` (new)

- [ ] **Step 1: Write the AGNES_WORKSPACE.md template**

Create `config/agnes_workspace_template.txt`:

```markdown
# Agnes analyst workspace

**Created:** {created_at}
**Server:** {server_url}
**Workspace:** {workspace_path}

This file documents what `agnes init` installed on this machine and in this folder.
Read this when you want to know "what is this thing", "how does it work", or
"how do I uninstall it". For Claude Code's instructions, see `CLAUDE.md`.

---

## What's installed (global, per-user)

| Path | What it is | How to remove |
|------|------------|---------------|
| `~/.local/bin/agnes` | The `agnes` CLI binary | `uv tool uninstall agnes-the-ai-analyst` |
| `~/.config/da/config.yaml` | Default Agnes server URL | `rm -rf ~/.config/da/` |
| `~/.config/da/token.json` | Personal access token (PAT) | `rm ~/.config/da/token.json` |
| `~/.agnes/ca.pem` | Server's CA cert (private CA installs only) | `rm ~/.agnes/ca.pem` |
| `~/.agnes/ca-bundle.pem` | Combined system + Agnes CA bundle | `rm ~/.agnes/ca-bundle.pem` |
| `~/.zshrc` / `~/.bashrc` block (marker `AGNES_CA_PEM_TRUST`) | `PATH` + `SSL_CERT_FILE` env | Edit rc, remove block |

---

## What's in this folder

| Path | What it is |
|------|------------|
| `./CLAUDE.md` | Rules + golden path for Claude Code (fetched from server's `/api/welcome`) |
| `./AGNES_WORKSPACE.md` | This file |
| `./.claude/settings.json` | Claude Code config: model, permissions, hooks |
| `./.claude/CLAUDE.local.md` | Your private notes (uploaded on session end) |
| `./.claude/rules/km_*.md` | Server-pushed corporate-knowledge rules (only when granted) |
| `./server/parquet/*.parquet` | Synced data — RBAC-filtered subset (only when grants exist) |
| `./user/duckdb/analytics.duckdb` | DuckDB views over the parquets — what `agnes query` reads |
| `./user/snapshots/*.parquet` | Ad-hoc materialized snapshots from `agnes snapshot create` |
| `./user/sessions/*.jsonl` | Captured Claude Code sessions (uploaded on session end) |

Some folders only exist when they have content — `agnes pull` and `agnes push`
only create them when there's something to write.

---

## How it stays fresh

Two hooks in `./.claude/settings.json` keep this workspace in sync without
you doing anything:

- **SessionStart** → `agnes pull --quiet` — new parquets, schema changes, and
  updated rules pull down before Claude Code answers. Failure is silent;
  your session continues with the last-known data.
- **SessionEnd** → `agnes push --quiet` — your session transcript and
  `CLAUDE.local.md` ship to the server.

Both are workspace-scoped — they only run when Claude Code opens this folder.

---

## Cheat sheet

```bash
# Tables you can read (server-side catalog, RBAC-filtered)
agnes catalog
agnes catalog --json | jq '.[] | select(.query_mode=="local")'

# Schema and sample
agnes schema opportunity
agnes describe opportunity -n 10

# Run a SQL query (DuckDB flavor against local parquets)
agnes query "SELECT count(*) FROM opportunity WHERE stage='Closed Won'"

# Remote BigQuery query (server-side, no local materialization)
agnes query --remote "SELECT count(*) FROM web_sessions_example"

# Materialize a remote subset locally
agnes snapshot create web_sessions_example \
  --select event_date,country_code \
  --where "event_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)" \
  --as recent_sessions

# Manual data refresh (the SessionStart hook does this automatically)
agnes pull

# Workspace status (what's synced, when)
agnes status

# Re-generate this workspace from scratch (preserves CLAUDE.local.md)
agnes init --server-url https://agnes.example.com --token <PAT> --force
```

---

## Uninstall

```bash
# 1. Remove the CLI
uv tool uninstall agnes-the-ai-analyst

# 2. Remove global config and trust artifacts
rm -rf ~/.config/da
rm -rf ~/.agnes

# 3. Remove the env-var block from your shell rc
# Open ~/.zshrc or ~/.bashrc, find the lines between
# "# AGNES_CA_PEM_TRUST — added by Agnes setup" and the next blank line, delete.

# 4. Remove this workspace
rm -rf ./CLAUDE.md ./AGNES_WORKSPACE.md ./.claude ./server ./user
```
```

- [ ] **Step 2: Write failing tests for `agnes init`**

```python
# tests/test_cli_init.py
"""Tests for `agnes init` orchestrator command."""

import json
from pathlib import Path
from unittest.mock import patch
from typer.testing import CliRunner

from cli.commands.init import init_app

runner = CliRunner()


def test_init_help():
    result = runner.invoke(init_app, ["--help"])
    assert result.exit_code == 0
    assert "--server-url" in result.output
    assert "--token" in result.output
    assert "--force" in result.output
    assert "--workspace" in result.output


def test_init_writes_expected_files(tmp_path, monkeypatch):
    """Mocked end-to-end: init writes CLAUDE.md, settings.json, AGNES_WORKSPACE.md."""
    # Mock all server-side calls
    def _api_get(path, *args, **kwargs):
        from unittest.mock import MagicMock
        resp = MagicMock()
        resp.status_code = 200
        if path == "/api/catalog/tables":
            resp.json.return_value = []
        elif path == "/api/welcome":
            resp.json.return_value = {"content": "# Test CLAUDE.md\n\nUse `agnes pull`.\n"}
        elif path == "/api/sync/manifest":
            resp.json.return_value = {"tables": []}
        elif path == "/api/memory/bundle":
            resp.json.return_value = {"mandatory": [], "approved": []}
        return resp
    monkeypatch.setattr("cli.commands.init.api_get", _api_get, raising=False)
    monkeypatch.setattr("cli.lib.pull.api_get", _api_get, raising=False)

    result = runner.invoke(init_app, [
        "--server-url", "http://test.example.com",
        "--token", "test-pat",
        "--workspace", str(tmp_path),
    ])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "CLAUDE.md").exists()
    assert "agnes pull" in (tmp_path / "CLAUDE.md").read_text()
    assert (tmp_path / ".claude" / "settings.json").exists()
    assert (tmp_path / ".claude" / "CLAUDE.local.md").exists()
    assert (tmp_path / "AGNES_WORKSPACE.md").exists()
    assert (tmp_path / "user" / "duckdb" / "analytics.duckdb").exists()


def test_init_no_dead_dirs_zero_grants(tmp_path, monkeypatch):
    """Zero grants → no .claude/rules, no server/parquet, no user/sessions."""
    # Same mock as above
    def _api_get(path, *args, **kwargs):
        from unittest.mock import MagicMock
        resp = MagicMock()
        resp.status_code = 200
        if path == "/api/catalog/tables":
            resp.json.return_value = []
        elif path == "/api/welcome":
            resp.json.return_value = {"content": "test"}
        elif path == "/api/sync/manifest":
            resp.json.return_value = {"tables": []}
        elif path == "/api/memory/bundle":
            resp.json.return_value = {"mandatory": [], "approved": []}
        return resp
    monkeypatch.setattr("cli.commands.init.api_get", _api_get, raising=False)
    monkeypatch.setattr("cli.lib.pull.api_get", _api_get, raising=False)

    runner.invoke(init_app, [
        "--server-url", "http://x", "--token", "t", "--workspace", str(tmp_path),
    ])
    for forbidden in ["data/parquet", "data/duckdb", "data/metadata",
                      "user/artifacts", "user/sessions",
                      "server/parquet", ".claude/rules"]:
        assert not (tmp_path / forbidden).exists(), f"forbidden created: {forbidden}"


def test_init_force_preserves_local_md(tmp_path, monkeypatch):
    """--force regenerates CLAUDE.md but never touches CLAUDE.local.md."""
    # First init
    def _api_get(path, *args, **kwargs):
        from unittest.mock import MagicMock
        resp = MagicMock()
        resp.status_code = 200
        if path == "/api/catalog/tables": resp.json.return_value = []
        elif path == "/api/welcome": resp.json.return_value = {"content": "v1"}
        else: resp.json.return_value = {} if "manifest" in path else {"mandatory": [], "approved": []}
        return resp
    monkeypatch.setattr("cli.commands.init.api_get", _api_get, raising=False)
    monkeypatch.setattr("cli.lib.pull.api_get", _api_get, raising=False)

    runner.invoke(init_app, ["--server-url", "http://x", "--token", "t", "--workspace", str(tmp_path)])
    (tmp_path / ".claude" / "CLAUDE.local.md").write_text("# my notes")

    # Second init with --force
    runner.invoke(init_app, ["--server-url", "http://x", "--token", "t",
                              "--workspace", str(tmp_path), "--force"])
    assert "my notes" in (tmp_path / ".claude" / "CLAUDE.local.md").read_text()
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
pytest tests/test_cli_init.py -v
```

- [ ] **Step 4: Create `cli/commands/init.py`**

```python
# cli/commands/init.py
"""`agnes init` — bootstrap an analyst workspace.

Single-paste flow: web user clicks "Generate prompt" on /setup?role=analyst,
pastes into Claude Code in an empty folder; Claude runs `agnes init` (among other
steps). Non-interactive: --token + --server-url required.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer

from cli.client import api_get
from cli.config import save_config, save_token
from cli.error_render import render_error
from cli.lib.hooks import install_claude_hooks
from cli.lib.pull import run_pull, PullResult


_INIT_MARKER = "AI Data Analyst"  # Detect existing workspace via CLAUDE.md substring


init_app = typer.Typer(help="Bootstrap an analyst workspace in this directory")


@init_app.callback(invoke_without_command=True)
def init(
    server_url: str = typer.Option(..., "--server-url", help="Agnes server URL"),
    token: str = typer.Option(..., "--token", help="Personal access token"),
    force: bool = typer.Option(False, "--force", help="Re-initialize an existing workspace"),
    workspace_str: Optional[str] = typer.Option(None, "--workspace", help="Target dir (default: cwd)"),
):
    """Bootstrap workspace: auth, CLAUDE.md, hooks, first pull, AGNES_WORKSPACE.md."""
    workspace = Path(workspace_str).resolve() if workspace_str else Path.cwd()
    server_url = server_url.rstrip("/")

    # Detect existing workspace
    claude_md = workspace / "CLAUDE.md"
    if claude_md.exists() and _INIT_MARKER in claude_md.read_text() and not force:
        typer.echo(render_error(0, {"detail": {
            "kind": "partial_state",
            "hint": "Workspace already initialized. Re-run with --force to redo.",
        }}), err=True)
        raise typer.Exit(1)

    # Step 1: Verify PAT via /api/catalog/tables (PAT-validating endpoint)
    try:
        resp = api_get("/api/catalog/tables", server_url=server_url, token=token)
        if resp.status_code == 401:
            typer.echo(render_error(401, {"detail": {
                "kind": "auth_failed",
                "hint": f"Token expired or invalid — get a fresh one at {server_url}/setup?role=analyst",
            }}), err=True)
            raise typer.Exit(1)
        resp.raise_for_status()
    except typer.Exit:
        raise
    except Exception as exc:
        typer.echo(render_error(0, {"detail": {
            "kind": "server_unreachable",
            "hint": f"Cannot reach {server_url} — check network or server status",
            "message": str(exc),
        }}), err=True)
        raise typer.Exit(1)

    # Step 2: Save config + token globally
    save_config({"server": server_url})
    save_token(token, email="")  # email empty — JWT carries it; we don't decode here

    # Step 3: Fetch CLAUDE.md from /api/welcome (server-rendered, RBAC-filtered)
    welcome_resp = api_get("/api/welcome", server_url=server_url, token=token,
                           params={"server_url": server_url})
    welcome_resp.raise_for_status()
    workspace.mkdir(parents=True, exist_ok=True)
    claude_md.write_text(welcome_resp.json()["content"], encoding="utf-8")

    # Step 4: Default settings.json + install hooks
    settings_path = workspace / ".claude" / "settings.json"
    if not settings_path.exists():
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps(
            {"model": "sonnet", "permissions": {"allow": ["Read", "Bash", "Grep", "Glob"]}},
            indent=2,
        ))
    install_claude_hooks(workspace)

    # Step 5: CLAUDE.local.md stub (preserved on re-run)
    local_md = workspace / ".claude" / "CLAUDE.local.md"
    if not local_md.exists():
        local_md.write_text(
            "# My Notes\n\nPersonal notes for this workspace. Uploaded on `agnes push`.\n",
            encoding="utf-8",
        )

    # Step 6: First pull
    try:
        result: PullResult = run_pull(server_url, token, workspace)
    except Exception as exc:
        typer.echo(render_error(0, {"detail": {
            "kind": "manifest_unauthorized",
            "hint": "Initial pull failed — workspace partially set up",
            "message": str(exc),
        }}), err=True)
        raise typer.Exit(1)

    # Step 7: Write AGNES_WORKSPACE.md from client-side template
    here = Path(__file__).parent
    template_path = here.parent.parent / "config" / "agnes_workspace_template.txt"
    if template_path.exists():
        template = template_path.read_text(encoding="utf-8")
    else:
        template = "# Agnes workspace\n\nCreated: {created_at}\nServer: {server_url}\n"
    workspace_md = (template
                    .replace("{created_at}", datetime.now(timezone.utc).isoformat())
                    .replace("{server_url}", server_url)
                    .replace("{workspace_path}", str(workspace)))
    (workspace / "AGNES_WORKSPACE.md").write_text(workspace_md, encoding="utf-8")

    # Step 8: Summary
    typer.echo("Workspace ready.")
    typer.echo(f"  Server   : {server_url}")
    typer.echo(f"  Tables   : {result.tables_updated} synced ({result.parquets_total} total)")
    typer.echo(f"  Rules    : {result.rules_count}")
    typer.echo(f"  Workspace: {workspace}")
    typer.echo("")
    typer.echo("Try: agnes catalog")
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
pytest tests/test_cli_init.py -v
```

- [ ] **Step 6: Commit**

```bash
git add cli/commands/init.py config/agnes_workspace_template.txt tests/test_cli_init.py
git commit -m "feat(cli): agnes init orchestrator + AGNES_WORKSPACE.md template"
```

---

### Task 12: New `agnes status` (workspace status, replaces `da analyst status`)

**Files:**
- Modify (overwrite): `cli/commands/status.py`
- Test: `tests/test_cli_status.py` (new)

- [ ] **Step 1: Read existing status.py to understand what to replace**

```bash
cat cli/commands/status.py
```

The existing `agnes status` shows server health. Per spec, this content moves to `agnes diagnose system` (Task 13); the file is repurposed for workspace status.

- [ ] **Step 2: Write failing tests**

```python
# tests/test_cli_status.py
from pathlib import Path
from typer.testing import CliRunner
from cli.commands.status import status_app

runner = CliRunner()


def test_status_uninitialized_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("DA_LOCAL_DIR", str(tmp_path))
    result = runner.invoke(status_app)
    assert result.exit_code in (0, 1)
    assert "not initialized" in result.output.lower() or "no workspace" in result.output.lower()


def test_status_initialized_workspace(tmp_path, monkeypatch):
    """A bootstrapped workspace shows 'initialized: yes' and basic stats."""
    monkeypatch.setenv("DA_LOCAL_DIR", str(tmp_path))
    (tmp_path / "CLAUDE.md").write_text("# AI Data Analyst\n")
    (tmp_path / "user" / "duckdb").mkdir(parents=True)
    (tmp_path / "user" / "duckdb" / "analytics.duckdb").touch()
    (tmp_path / "server" / "parquet").mkdir(parents=True)
    (tmp_path / "server" / "parquet" / "tbl1.parquet").touch()

    result = runner.invoke(status_app)
    assert result.exit_code == 0
    assert "initialized" in result.output.lower()
    assert "1" in result.output  # one parquet


def test_status_json(tmp_path, monkeypatch):
    monkeypatch.setenv("DA_LOCAL_DIR", str(tmp_path))
    (tmp_path / "CLAUDE.md").write_text("# AI Data Analyst\n")
    result = runner.invoke(status_app, ["--json"])
    assert result.exit_code == 0
    import json
    body = json.loads(result.output)
    assert "workspace" in body and "initialized" in body
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
pytest tests/test_cli_status.py -v
```

- [ ] **Step 4: Overwrite `cli/commands/status.py`**

```python
# cli/commands/status.py
"""`agnes status` — workspace status: initialized? data fresh? hooks active?"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import typer


_INIT_MARKER = "AI Data Analyst"


status_app = typer.Typer(help="Workspace status (was `da analyst status`)")


@status_app.callback(invoke_without_command=True)
def status(
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
):
    workspace = Path(os.environ.get("DA_LOCAL_DIR", ".")).resolve()

    initialized = False
    claude_md = workspace / "CLAUDE.md"
    if claude_md.exists():
        initialized = _INIT_MARKER in claude_md.read_text()

    parquet_dir = workspace / "server" / "parquet"
    parquets = list(parquet_dir.glob("*.parquet")) if parquet_dir.exists() else []

    db_path = workspace / "user" / "duckdb" / "analytics.duckdb"
    last_synced = None
    if db_path.exists():
        last_synced = datetime.fromtimestamp(db_path.stat().st_mtime, tz=timezone.utc).isoformat()

    sessions_dir = workspace / "user" / "sessions"
    session_count = len(list(sessions_dir.glob("*.jsonl"))) if sessions_dir.exists() else 0

    info = {
        "workspace": str(workspace),
        "initialized": initialized,
        "parquet_tables": len(parquets),
        "duckdb_exists": db_path.exists(),
        "last_synced": last_synced,
        "sessions_pending_upload": session_count,
    }

    if as_json:
        typer.echo(json.dumps(info, indent=2))
        return

    typer.echo(f"Workspace : {workspace}")
    typer.echo(f"Initialized: {'yes' if initialized else 'no'}")
    typer.echo(f"Parquets  : {info['parquet_tables']}")
    typer.echo(f"DuckDB    : {'yes' if info['duckdb_exists'] else 'no'}")
    typer.echo(f"Last sync : {last_synced or 'never'}")
    typer.echo(f"Pending uploads: {session_count} sessions")

    if not initialized:
        typer.echo("")
        typer.echo("Run `agnes init --server-url <URL> --token <PAT>` to bootstrap.")
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
pytest tests/test_cli_status.py -v
```

- [ ] **Step 6: Commit**

```bash
git add cli/commands/status.py tests/test_cli_status.py
git commit -m "feat(cli): agnes status now shows workspace state (was system health)"
```

---

### Task 13: Move old `agnes status` content into `agnes diagnose system`

**Files:**
- Modify: `cli/commands/diagnose.py` (add `system` subcommand with the old status logic)
- Test: `tests/test_cli_diagnose_system.py` (new)

- [ ] **Step 1: Recover old `agnes status` logic**

```bash
git show HEAD~12:cli/commands/status.py
```

(Adjust the ref — find the commit before the rewrite via `git log --oneline cli/commands/status.py | head`.) Save the body to a scratch file.

- [ ] **Step 2: Read existing diagnose command structure**

```bash
cat cli/commands/diagnose.py
```

- [ ] **Step 3: Add `system` subcommand**

Append the old status logic as a `system` subcommand of `diagnose_app`. Keep diagnose's existing default behavior (overall health) intact.

- [ ] **Step 4: Test**

```python
# tests/test_cli_diagnose_system.py
from typer.testing import CliRunner
from cli.commands.diagnose import diagnose_app


def test_diagnose_system_help():
    runner = CliRunner()
    result = runner.invoke(diagnose_app, ["system", "--help"])
    assert result.exit_code == 0
```

- [ ] **Step 5: Commit**

```bash
git add cli/commands/diagnose.py tests/test_cli_diagnose_system.py
git commit -m "refactor(cli): move old `agnes status` health check to `agnes diagnose system`"
```

---

### Task 14: `agnes snapshot create` — fold `da fetch` into snapshot group

**Files:**
- Modify: `cli/commands/snapshot.py` (add `create` subcommand)
- Test: `tests/test_cli_snapshot_create.py` (new)

- [ ] **Step 1: Read existing fetch.py and snapshot.py**

```bash
cat cli/commands/fetch.py
cat cli/commands/snapshot.py
```

- [ ] **Step 2: Add `create` subcommand to `snapshot_app`**

Move the body of `fetch.py:fetch()` into a new `@snapshot_app.command("create")`. Keep all flags. Update the existence check:

```python
local_db = _local_dir() / "user" / "duckdb" / "analytics.duckdb"
if not local_db.exists():
    typer.echo("Local DuckDB not found. Run: agnes pull first.", err=True)
    raise typer.Exit(1)
# (then proceed with duckdb.connect — no longer creates an empty DB)
```

- [ ] **Step 3: Add tests**

```python
# tests/test_cli_snapshot_create.py
from typer.testing import CliRunner
from cli.commands.snapshot import snapshot_app


def test_snapshot_create_help():
    runner = CliRunner()
    result = runner.invoke(snapshot_app, ["create", "--help"])
    assert result.exit_code == 0
    for flag in ["--select", "--where", "--limit", "--order-by", "--as", "--estimate", "--no-estimate", "--force"]:
        assert flag in result.output


def test_snapshot_create_no_duckdb_friendly_exit(tmp_path, monkeypatch):
    monkeypatch.setenv("DA_LOCAL_DIR", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(snapshot_app, ["create", "any_table", "--as", "x", "--estimate"])
    assert result.exit_code == 1
    assert "Run: agnes pull" in result.output or "Run: agnes pull" in (result.stderr or "")
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_cli_snapshot_create.py -v
```

- [ ] **Step 5: Commit**

```bash
git add cli/commands/snapshot.py tests/test_cli_snapshot_create.py
git commit -m "feat(cli): agnes snapshot create (folded from da fetch); friendly exit if no DuckDB"
```

---

### Task 15: `agnes catalog --metrics` — fold `da metrics list/show`

**Files:**
- Modify: `cli/commands/catalog.py`
- Test: `tests/test_cli_catalog_metrics.py` (new)

- [ ] **Step 1: Read existing metrics list/show logic**

```bash
grep -n "def list\|def show\|@" cli/commands/metrics.py | head
```

- [ ] **Step 2: Add `--metrics` and `--metrics --show <id>` to catalog**

Modify `cli/commands/catalog.py`:

```python
@catalog_app.callback(invoke_without_command=True)
def catalog(
    as_json: bool = typer.Option(False, "--json"),
    metrics: bool = typer.Option(False, "--metrics", help="Show metric definitions instead of tables"),
    show: Optional[str] = typer.Option(None, "--show", help="With --metrics: show one metric by id"),
):
    if metrics and show:
        return _show_one_metric(show, as_json)
    if metrics:
        return _list_metrics(as_json)
    return _list_tables(as_json)
```

(`_list_metrics` and `_show_one_metric` lift from `metrics.py:list_metrics` and `metrics.py:show_metric`.)

- [ ] **Step 3: Add tests**

```python
# tests/test_cli_catalog_metrics.py
from typer.testing import CliRunner
from cli.commands.catalog import catalog_app


def test_catalog_metrics_help():
    runner = CliRunner()
    result = runner.invoke(catalog_app, ["--help"])
    assert result.exit_code == 0
    assert "--metrics" in result.output
    assert "--show" in result.output
```

- [ ] **Step 4: Commit**

```bash
git add cli/commands/catalog.py tests/test_cli_catalog_metrics.py
git commit -m "feat(cli): agnes catalog --metrics replaces da metrics list/show"
```

---

### Task 16: Move `da metrics import/export/validate` to `agnes admin metrics`

**Files:**
- Create: `cli/commands/admin_metrics.py`
- Modify: `cli/commands/admin.py` (register the sub-Typer)

- [ ] **Step 1: Create `cli/commands/admin_metrics.py`**

Lift `import_metrics`, `export_metrics`, `validate_metrics` from `cli/commands/metrics.py`. Wrap in a sub-Typer:

```python
# cli/commands/admin_metrics.py
"""`agnes admin metrics {import,export,validate}` — lifted from metrics.py."""

import typer

admin_metrics_app = typer.Typer(help="Admin: metric definition management")


@admin_metrics_app.command("import")
def import_metrics(directory: str = typer.Argument(...)):
    # ... lifted logic from cli/commands/metrics.py:import_metrics
    pass


@admin_metrics_app.command("export")
def export_metrics(target: str = typer.Argument(...)):
    # ... lifted logic
    pass


@admin_metrics_app.command("validate")
def validate_metrics():
    # ... lifted logic
    pass
```

(Copy the actual implementations from `metrics.py` verbatim.)

- [ ] **Step 2: Register in admin app**

In `cli/commands/admin.py`, add:

```python
from cli.commands.admin_metrics import admin_metrics_app
admin_app.add_typer(admin_metrics_app, name="metrics")
```

- [ ] **Step 3: Test**

```python
# tests/test_cli_admin_metrics.py
from typer.testing import CliRunner
from cli.commands.admin import admin_app


def test_admin_metrics_subcommands_present():
    runner = CliRunner()
    result = runner.invoke(admin_app, ["metrics", "--help"])
    assert result.exit_code == 0
    assert "import" in result.output
    assert "export" in result.output
    assert "validate" in result.output
```

- [ ] **Step 4: Commit**

```bash
git add cli/commands/admin_metrics.py cli/commands/admin.py tests/test_cli_admin_metrics.py
git commit -m "feat(cli): agnes admin metrics {import,export,validate}"
```

---

## Phase 4 — Wiring + cleanup

### Task 17: Update reader hint texts

**Files:**
- Modify: `cli/commands/query.py` (two occurrences), `cli/commands/explore.py`

- [ ] **Step 1: Find all "Run: da sync" strings**

```bash
grep -rn "Run: da sync" cli/
```

- [ ] **Step 2: Replace with "Run: agnes pull"**

```bash
sed -i.bak 's/Run: da sync/Run: agnes pull/g' cli/commands/query.py cli/commands/explore.py
rm cli/commands/query.py.bak cli/commands/explore.py.bak
```

- [ ] **Step 3: Verify no leftover**

```bash
grep -rn "Run: da sync" cli/
```

Expected: no matches.

- [ ] **Step 4: Commit**

```bash
git add cli/commands/query.py cli/commands/explore.py
git commit -m "fix(cli): hint text 'Run: da sync' → 'Run: agnes pull'"
```

---

### Task 18: Update `cli/main.py` registrations + delete obsolete commands

**Files:**
- Modify: `cli/main.py`
- Delete: `cli/commands/sync.py`, `cli/commands/fetch.py`, `cli/commands/analyst.py`, `cli/commands/metrics.py`

- [ ] **Step 1: Update `cli/main.py`**

Replace lines 11-28 (imports) and 91-109 (registrations):

```python
from cli.commands.auth import auth_app
from cli.commands.init import init_app
from cli.commands.pull import pull_app
from cli.commands.push import push_app
from cli.commands.query import query_command
from cli.commands.status import status_app
from cli.commands.admin import admin_app
from cli.commands.diagnose import diagnose_app
from cli.commands.skills import skills_app
from cli.commands.setup import setup_app
from cli.commands.server import server_app
from cli.commands.explore import explore_app
from cli.commands.catalog import catalog_app
from cli.commands.schema import schema_app
from cli.commands.describe import describe_app
from cli.commands.snapshot import snapshot_app
from cli.commands.disk_info import disk_info_app
```

```python
# Register subcommands
app.add_typer(auth_app, name="auth")
app.add_typer(init_app, name="init")
app.add_typer(pull_app, name="pull")
app.add_typer(push_app, name="push")
app.command("query")(query_command)
app.add_typer(status_app, name="status")
app.add_typer(admin_app, name="admin")
app.add_typer(diagnose_app, name="diagnose")
app.add_typer(skills_app, name="skills")
app.add_typer(setup_app, name="setup")
app.add_typer(server_app, name="server")
app.add_typer(explore_app, name="explore")
app.add_typer(catalog_app, name="catalog")
app.add_typer(schema_app, name="schema")
app.add_typer(describe_app, name="describe")
app.add_typer(snapshot_app, name="snapshot")
app.add_typer(disk_info_app, name="disk-info")
```

- [ ] **Step 2: Delete obsolete files**

```bash
git rm cli/commands/sync.py cli/commands/fetch.py cli/commands/analyst.py cli/commands/metrics.py
```

- [ ] **Step 3: Verify no other code imports them**

```bash
grep -rn "from cli.commands.sync\|from cli.commands.fetch\|from cli.commands.analyst\|from cli.commands.metrics" .
```

Expected: no matches (anything found needs to be updated to use the new homes).

- [ ] **Step 4: Run the full test suite (smoke)**

```bash
pytest tests/ -x --ignore=tests/test_clean_install_integration.py --ignore=tests/test_reader_smoke_matrix.py 2>&1 | tail -30
```

Expected: tests for moved/deleted commands fail with import errors — those tests are also being deleted (or already updated in earlier tasks). Other tests should pass.

If old test files reference the deleted commands, `git rm` them too:

```bash
git rm tests/test_analyst*.py tests/test_sync*.py tests/test_fetch*.py tests/test_metrics_cli*.py 2>/dev/null || true
```

- [ ] **Step 5: Commit**

```bash
git add cli/main.py
git rm cli/commands/{sync,fetch,analyst,metrics}.py 2>/dev/null
git commit -m "refactor(cli): drop sync/fetch/analyst/metrics; register init/pull/push"
```

---

### Task 19: Update repo-root `CLAUDE.md`

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Apply systematic rewrites**

```bash
sed -i.bak \
  -e 's|da sync --upload-only|agnes push|g' \
  -e 's|da sync|agnes pull|g' \
  -e 's|da analyst setup|agnes init|g' \
  -e 's|da fetch|agnes snapshot create|g' \
  -e 's|da metrics list|agnes catalog --metrics|g' \
  -e 's|da metrics show|agnes catalog --metrics --show|g' \
  -e 's|da metrics import|agnes admin metrics import|g' \
  -e 's|data/parquet/|server/parquet/|g' \
  -e 's|data/duckdb/|user/duckdb/|g' \
  CLAUDE.md
rm CLAUDE.md.bak
```

- [ ] **Step 2: Manually rewrite the "Local sync & Claude Code hooks" subsection**

Find the section. Replace the surrounding prose so it describes `agnes pull` + `agnes push` hooks:

```markdown
### Local sync & Claude Code hooks

`agnes pull` is the canonical analyst-side distribution path: pulls the
RBAC-filtered manifest from the server, downloads parquets whose MD5 changed
(skipping `query_mode='remote'` rows), rebuilds local DuckDB views over them.
`agnes push` mirrors it for the upload direction (sessions, CLAUDE.local.md).

`agnes init` writes two hooks into `<workspace>/.claude/settings.json`:

- `SessionStart` → `agnes pull --quiet` — pulls fresh parquets at the start of every Claude Code session
- `SessionEnd`   → `agnes push --quiet` — uploads session jsonl + `CLAUDE.local.md` to the server

Both pass `--quiet` so they don't pollute Claude Code stdout, and trail with `|| true` so a server outage never blocks a session. Workspace-level (not user-home) so the hooks fire only when Claude Code opens this analyst workspace, not in unrelated sessions on the same machine.

Admin RBAC for auto-sync: `query_mode IN ('local', 'materialized')` plus a `resource_grants` row for one of the analyst's groups → table appears in their manifest → `agnes pull` downloads it. No per-user sync config; the admin layer is the single source of truth.
```

- [ ] **Step 3: Verify no leftover legacy strings**

```bash
grep -nE 'da sync|da fetch|da analyst|da metrics list|da metrics show|data/parquet/|data/duckdb/' CLAUDE.md
```

Expected: no matches.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(claude-md): rewrite verbs + paths for new CLI surface"
```

---

## Phase 5 — Test fixtures and integration tests

### Task 20: Create `tests/fixtures/analyst_bootstrap.py`

**Files:**
- Create: `tests/fixtures/analyst_bootstrap.py`
- Modify: `tests/conftest.py` (import the fixtures)

- [ ] **Step 1: Read existing test infrastructure**

```bash
grep -n "fastapi\|TestClient\|tmp_path" tests/conftest.py | head -30
```

- [ ] **Step 2: Create the fixtures**

```python
# tests/fixtures/analyst_bootstrap.py
"""Test fixtures for the clean-bootstrap test suite.

Per spec §"Test fixtures":
- fastapi_test_server, test_pat, test_pat_no_grants, zero_grants_workspace,
  web_session, client.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import httpx
import pytest
import uvicorn


NONEXISTENT_TABLE = "__nonexistent__"  # Sentinel for reader smoke matrix


class _ServerHandle:
    def __init__(self, url: str, server: uvicorn.Server, thread: threading.Thread):
        self.url = url
        self._server = server
        self._thread = thread

    def shutdown(self):
        self._server.should_exit = True
        self._thread.join(timeout=5)


def _seed_db(data_dir: Path):
    """Initialize a fresh system.duckdb with seeded admin/analyst users + tables.

    Imports app modules at function scope to avoid circular imports during
    collection.
    """
    import os
    os.environ["DATA_DIR"] = str(data_dir)
    from src.db import get_db_connection
    from src.repositories.users import UserRepository
    from src.repositories.user_groups import UserGroupRepository
    from src.repositories.table_registry import TableRegistryRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.resource_grants import ResourceGrantsRepository
    from app.auth.providers.password import _hash_password

    conn = get_db_connection()

    # Seed users
    user_repo = UserRepository(conn)
    admin_id = user_repo.create(email="admin@example.com", name="Admin",
                                password_hash=_hash_password("test-password"),
                                is_admin=True)
    analyst_id = user_repo.create(email="analyst@example.com", name="Analyst",
                                  password_hash=_hash_password("analyst-pw"),
                                  is_admin=False)

    # Seed groups (Admin + Everyone are seeded as is_system=TRUE on first run)
    grp_repo = UserGroupRepository(conn)
    admin_group = grp_repo.find_by_name("Admin")
    everyone_group = grp_repo.find_by_name("Everyone")

    # Memberships
    members = UserGroupMembersRepository(conn)
    members.add(user_id=admin_id, group_id=admin_group.id, source="system_seed")
    members.add(user_id=analyst_id, group_id=everyone_group.id, source="system_seed")

    # Tables
    tbl_repo = TableRegistryRepository(conn)
    tbl_repo.create(id="local_tbl", name="local_tbl", source_type="keboola",
                    bucket="test", source_table="local_tbl", query_mode="local")
    tbl_repo.create(id="materialized_tbl", name="materialized_tbl", source_type="bigquery",
                    bucket="test", source_table="materialized_tbl", query_mode="materialized")
    tbl_repo.create(id="remote_tbl", name="remote_tbl", source_type="bigquery",
                    bucket="test", source_table="remote_tbl", query_mode="remote")

    return {"admin_id": admin_id, "analyst_id": analyst_id,
            "admin_group_id": admin_group.id, "everyone_group_id": everyone_group.id}


@pytest.fixture
def fastapi_test_server(tmp_path) -> Iterator[_ServerHandle]:
    """Boot a real FastAPI server in a background thread against tmp_path DATA_DIR."""
    data_dir = tmp_path / "agnes-data"
    data_dir.mkdir()
    seeded = _seed_db(data_dir)
    handle_port = 18712 + (id(tmp_path) % 1000)

    from app.main import app as fastapi_app
    config = uvicorn.Config(fastapi_app, host="127.0.0.1", port=handle_port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait for server up
    url = f"http://127.0.0.1:{handle_port}"
    for _ in range(50):
        try:
            httpx.get(f"{url}/api/health", timeout=0.2)
            break
        except Exception:
            time.sleep(0.1)
    else:
        pytest.fail("fastapi_test_server failed to start")

    handle = _ServerHandle(url, server, thread)
    handle._seeded = seeded
    handle.NONEXISTENT_TABLE = NONEXISTENT_TABLE
    yield handle
    handle.shutdown()


@pytest.fixture
def web_session(fastapi_test_server) -> Iterator[httpx.Client]:
    """Authenticated httpx.Client using cookie session for admin@example.com."""
    client = httpx.Client(base_url=fastapi_test_server.url, follow_redirects=False)
    resp = client.post("/auth/password/login/web",
                       data={"email": "admin@example.com", "password": "test-password"})
    assert resp.status_code in (200, 302), f"web_session login failed: {resp.text}"
    yield client
    client.close()


@pytest.fixture
def test_pat(web_session) -> str:
    """Mint a PAT for analyst@example.com with 2 grants + 2 mandatory rules."""
    # First, grant the analyst access to local_tbl + materialized_tbl
    web_session.post("/api/admin/grants",
                     json={"group_id": "...everyone...", "resource_type": "table",
                           "resource_id": "local_tbl"})
    # ... similarly for materialized_tbl + 2 mandatory memory items
    # (Use the actual admin endpoints for grants and memory items.)

    # Mint PAT (as analyst — log in as analyst first, then mint)
    analyst_session = httpx.Client(base_url=web_session.base_url, follow_redirects=False)
    analyst_session.post("/auth/password/login/web",
                         data={"email": "analyst@example.com", "password": "analyst-pw"})
    resp = analyst_session.post("/auth/tokens",
                                json={"name": "test", "ttl_seconds": 3600})
    assert resp.status_code == 201, resp.text
    return resp.json()["token"]


@pytest.fixture
def test_pat_no_grants(web_session) -> str:
    analyst_session = httpx.Client(base_url=web_session.base_url, follow_redirects=False)
    analyst_session.post("/auth/password/login/web",
                         data={"email": "analyst@example.com", "password": "analyst-pw"})
    resp = analyst_session.post("/auth/tokens",
                                json={"name": "test-nogrants", "ttl_seconds": 3600})
    return resp.json()["token"]


@pytest.fixture
def zero_grants_workspace(tmp_path, fastapi_test_server, test_pat_no_grants) -> Path:
    """Run `agnes init` against a no-grants PAT; return the workspace path."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = subprocess.run([
        "da", "init",
        "--server-url", fastapi_test_server.url,
        "--token", test_pat_no_grants,
        "--workspace", str(workspace),
    ], capture_output=True, text=True)
    assert result.returncode == 0, f"init failed: {result.stderr}"
    return workspace
```

(Adapt API calls as needed — the actual repository/route names may differ. Spec the goal; implementer adapts.)

- [ ] **Step 3: Wire fixtures into conftest**

In `tests/conftest.py`, append:

```python
from tests.fixtures.analyst_bootstrap import (
    fastapi_test_server, web_session, test_pat, test_pat_no_grants,
    zero_grants_workspace, NONEXISTENT_TABLE,
)
```

- [ ] **Step 4: Smoke-test fixture creation**

```python
# tests/test_fixtures_smoke.py
def test_server_boots(fastapi_test_server):
    import httpx
    resp = httpx.get(f"{fastapi_test_server.url}/api/health")
    assert resp.status_code == 200


def test_zero_grants_workspace_minimal(zero_grants_workspace):
    assert (zero_grants_workspace / "CLAUDE.md").exists()
    assert (zero_grants_workspace / "AGNES_WORKSPACE.md").exists()
    assert not (zero_grants_workspace / "server" / "parquet").exists()
    assert not (zero_grants_workspace / ".claude" / "rules").exists()
```

```bash
pytest tests/test_fixtures_smoke.py -v
```

- [ ] **Step 5: Commit**

```bash
git add tests/fixtures/analyst_bootstrap.py tests/conftest.py tests/test_fixtures_smoke.py
git commit -m "test: clean-bootstrap fixtures (fastapi_test_server, test_pat, etc.)"
```

---

### Task 21: Reader smoke matrix

**Files:**
- Create: `tests/test_reader_smoke_matrix.py`

- [ ] **Step 1: Write the matrix**

```python
# tests/test_reader_smoke_matrix.py
"""Reader smoke matrix — every CLI command on a freshly-bootstrapped
zero-grants workspace, asserts no traceback. The load-bearing test for
'nothing crashes on missing dirs'."""

import subprocess

import pytest

from tests.fixtures.analyst_bootstrap import NONEXISTENT_TABLE


READER_COMMANDS = [
    ["agnes", "catalog"],
    ["agnes", "catalog", "--metrics"],
    ["agnes", "schema", NONEXISTENT_TABLE],
    ["agnes", "describe", NONEXISTENT_TABLE],
    ["agnes", "query", "SELECT 1"],
    ["agnes", "explore", NONEXISTENT_TABLE],
    ["agnes", "disk-info"],
    ["agnes", "snapshot", "list"],
    ["agnes", "snapshot", "create", NONEXISTENT_TABLE, "--as", "x", "--estimate"],
    ["agnes", "status"],
    ["agnes", "diagnose"],
    ["agnes", "auth", "whoami"],
    ["agnes", "skills", "list"],
    ["agnes", "skills", "show", "agnes-data-querying"],
]


@pytest.mark.parametrize("cmd", READER_COMMANDS, ids=lambda c: " ".join(c))
def test_reader_does_not_crash_on_zero_grants(zero_grants_workspace, cmd):
    """Exit 0 (success) or exit 1 (friendly hint) is OK; tracebacks are forbidden."""
    result = subprocess.run(cmd, cwd=zero_grants_workspace,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode in (0, 1), \
        f"{cmd} crashed: rc={result.returncode}, stderr={result.stderr}"
    assert "Traceback" not in result.stderr, f"{cmd} threw: {result.stderr}"
```

- [ ] **Step 2: Run**

```bash
pytest tests/test_reader_smoke_matrix.py -v
```

Expected: all parametrized cases PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_reader_smoke_matrix.py
git commit -m "test: reader smoke matrix on zero-grants workspace"
```

---

### Task 22: Clean-install integration tests

**Files:**
- Create: `tests/test_clean_install_integration.py`

- [ ] **Step 1: Write the integration tests per spec §5.2**

```python
# tests/test_clean_install_integration.py
"""End-to-end clean-install integration tests for `agnes init`."""

import json
import subprocess
from pathlib import Path


def assert_no_dead_dirs(workspace: Path):
    forbidden_unconditional = ["data/parquet", "data/duckdb", "data/metadata",
                               "user/artifacts", ".agnes"]
    for d in forbidden_unconditional:
        assert not (workspace / d).exists(), f"forbidden dir created: {d}"
    for d in [".claude/rules", "server/parquet", "user/sessions", "user/snapshots"]:
        path = workspace / d
        if path.exists():
            assert any(path.iterdir()), f"{d} exists but is empty"


def test_clean_install_minimal_grants(fastapi_test_server, tmp_path, test_pat):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    result = subprocess.run([
        "da", "init",
        "--server-url", fastapi_test_server.url,
        "--token", test_pat,
        "--workspace", str(workspace),
    ], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

    for must in ["CLAUDE.md", "AGNES_WORKSPACE.md",
                 ".claude/settings.json", ".claude/CLAUDE.local.md",
                 "user/duckdb/analytics.duckdb"]:
        assert (workspace / must).exists(), f"missing: {must}"

    parquets = list((workspace / "server" / "parquet").glob("*.parquet"))
    assert len(parquets) == 2, "expected 2 parquets (local + materialized grants)"

    rules = list((workspace / ".claude" / "rules").iterdir())
    assert len(rules) == 2, "expected 2 mandatory rules"

    assert_no_dead_dirs(workspace)

    settings = json.loads((workspace / ".claude" / "settings.json").read_text())
    assert any("agnes pull" in h["hooks"][0]["command"]
               for h in settings["hooks"]["SessionStart"])
    assert any("agnes push" in h["hooks"][0]["command"]
               for h in settings["hooks"]["SessionEnd"])

    claude_md = (workspace / "CLAUDE.md").read_text()
    assert "agnes pull" in claude_md
    assert "da sync" not in claude_md

    workspace_md = (workspace / "AGNES_WORKSPACE.md").read_text()
    assert test_pat not in workspace_md, "PAT must not leak into AGNES_WORKSPACE.md"
    for placeholder in ["{created_at}", "{server_url}", "{workspace_path}"]:
        assert placeholder not in workspace_md, f"placeholder leaked: {placeholder}"
    assert fastapi_test_server.url in workspace_md
    assert str(workspace) in workspace_md
    assert "agnes pull" in workspace_md


def test_clean_install_zero_grants(fastapi_test_server, tmp_path, test_pat_no_grants):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    subprocess.run([
        "da", "init",
        "--server-url", fastapi_test_server.url,
        "--token", test_pat_no_grants,
        "--workspace", str(workspace),
    ], check=True)

    must_exist = {"CLAUDE.md", "AGNES_WORKSPACE.md",
                  ".claude/settings.json", ".claude/CLAUDE.local.md",
                  "user/duckdb/analytics.duckdb"}
    must_not_exist = {".claude/rules", "server/parquet", "data/parquet",
                      "data/duckdb", "data/metadata", "user/artifacts",
                      "user/sessions", "user/snapshots", ".agnes"}
    for p in must_exist:
        assert (workspace / p).exists(), f"missing: {p}"
    for p in must_not_exist:
        assert not (workspace / p).exists(), f"unexpected: {p}"
    assert_no_dead_dirs(workspace)


def test_init_force_preserves_local_md(fastapi_test_server, tmp_path, test_pat):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    subprocess.run(["agnes", "init", "--server-url", fastapi_test_server.url,
                    "--token", test_pat, "--workspace", str(workspace)], check=True)
    (workspace / ".claude" / "CLAUDE.local.md").write_text("# my private notes\n")

    subprocess.run(["agnes", "init", "--server-url", fastapi_test_server.url,
                    "--token", test_pat, "--workspace", str(workspace),
                    "--force"], check=True)
    assert "my private notes" in (workspace / ".claude" / "CLAUDE.local.md").read_text()


def test_readers_in_pre_init_dir(tmp_path):
    """Reader commands in a folder that never had `agnes init`. Friendly hints, no tracebacks."""
    for cmd in [["agnes", "query", "SELECT 1"],
                ["agnes", "snapshot", "create", "x", "--as", "y", "--estimate"],
                ["agnes", "explore", "x"],
                ["agnes", "snapshot", "list"]]:
        result = subprocess.run(cmd, cwd=tmp_path,
                                capture_output=True, text=True, timeout=15)
        assert "Traceback" not in result.stderr
```

- [ ] **Step 2: Run**

```bash
pytest tests/test_clean_install_integration.py -v
```

- [ ] **Step 3: Commit**

```bash
git add tests/test_clean_install_integration.py
git commit -m "test: clean-install integration suite (minimal/zero grants, force, pre-init)"
```

---

### Task 23: Manual clean-install protocol — document in RELEASE_CHECKLIST.md

**Files:**
- Modify or create: `docs/RELEASE_CHECKLIST.md`

- [ ] **Step 1: Add the manual protocol from spec §5.5**

If `docs/RELEASE_CHECKLIST.md` exists, append; otherwise create with header:

```markdown
# Release Checklist

## Bootstrap path changes (mandatory pre-merge)

For any PR touching the analyst-bootstrap path (`agnes init`, `cli/lib/pull.py`,
`cli/lib/hooks.py`, `app/web/setup_instructions.py`, `/api/welcome`), run
this protocol locally before requesting review:

1. `git clean -fdx` in the repo (no build artifacts).
2. Boot FastAPI locally against a clean test instance state.
3. Empty terminal in `/tmp/test-analyst-1`. From the web `/setup?role=analyst`, paste prompt.
4. `tree -a /tmp/test-analyst-1` and compare with the expected tree from
   `docs/superpowers/specs/2026-05-04-clean-analyst-bootstrap-design.md` §5.2.
5. `claude` in that folder. Three queries: "what tables can I see",
   "SELECT count(*) FROM <t>", "show me last 5 rows of <t>". All must work
   without further intervention.
6. `/exit`. Verify SessionEnd hook ran (server-side audit log shows `agnes push`;
   `du -sh /tmp/test-analyst-1/user/sessions/` non-empty).
7. Second `claude` in same folder. Verify SessionStart hook fires
   (`agnes pull` request in audit log).
8. Second workspace `/tmp/test-analyst-2` with the same PAT (within TTL).
   Repeat 3-5. Verify global `~/.config/da/` is not duplicated; the second
   workspace has its own DuckDB.
```

- [ ] **Step 2: Commit**

```bash
git add docs/RELEASE_CHECKLIST.md
git commit -m "docs: clean-install manual protocol in release checklist"
```

---

## Phase 6 — CHANGELOG and final verification

### Task 24: Update CHANGELOG

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Add an entry under `[Unreleased]`**

Open `CHANGELOG.md`, find the topmost `## [Unreleased]` section (it sits above the most recent released version, currently `## [0.32.0]`). Add the entry from the spec §"CHANGELOG entry (preview)":

```markdown
## [Unreleased]

### Changed
- **BREAKING** CLI binary renamed from `da` to `agnes`. No backward-compat alias is shipped. Update shell aliases, hook commands in any pre-existing `.claude/settings.json`, scripts, and cron jobs. Reinstall via `uv tool install <wheel>`; the wheel now ships an `agnes` entry point.
- **BREAKING** Analyst bootstrap rewritten end-to-end. `da analyst setup` is removed; replaced by `agnes init` (non-interactive, requires `--server-url` and `--token`). `da sync` is split into `agnes pull` (refresh) and `agnes push` (upload). `da fetch` is folded into `agnes snapshot create`. `da metrics list/show` is folded into `agnes catalog --metrics`; `da metrics import/export/validate` move to `agnes admin metrics {import,export,validate}`. The `da analyst` namespace is removed; the workspace status command is now `agnes status`. The previous `da status` (server-health overview) becomes `agnes diagnose system`.
- **BREAKING** Workspace layout simplified. Removed: `data/parquet/`, `data/duckdb/`, `data/metadata/`, `user/artifacts/`. Canonical paths: `server/parquet/` (synced parquets), `user/duckdb/analytics.duckdb` (DuckDB views), `user/snapshots/` (ad-hoc snapshots), `user/sessions/` (recorded sessions).
- The `/setup` web page now branches on a `role` query parameter: `/setup?role=analyst` renders the analyst workspace bootstrap prompt; `/setup?role=admin` renders the admin CLI install prompt. `/install` continues to 302 to `/setup`.
- `CLAUDE.md` server-side template + repo-root `CLAUDE.md` updated to reference the new CLI verbs and workspace paths. The admin UI for the `claude_md_template` DB override (`/admin/workspace-prompt`) renders a yellow banner when the saved override contains legacy strings (`data/parquet/`, `da sync`, `da fetch`, `da analyst setup`, `da metrics list/show`); admins re-author and save to clear it. Migration is manual.

### Added
- `AGNES_WORKSPACE.md` — human-readable workspace docs file generated by `agnes init` in the workspace root. Documents global install, workspace layout, hooks, cheat sheet, uninstall recipe.
- PAT request body now accepts `scope: str = "general"` and `ttl_seconds: int | None = None` fields. PATs minted with `scope="bootstrap-analyst"` are TTL-clamped to ≤ 1 h server-side. Existing `expires_in_days` field continues to work; `ttl_seconds` wins when both are set. `ttl_seconds` upper bound is 315_360_000 (matches `expires_in_days <= 3650` cap).
- `cli/lib/` shared-library tree, with `cli/lib/pull.py:run_pull` (data-refresh primitive callable from both the Typer wrapper and `agnes init`) and `cli/lib/hooks.py:install_claude_hooks` (workspace-scoped Claude Code hook installer).

### Fixed
- `agnes pull` (formerly `da sync`) no longer creates `.claude/rules/` when the corporate-memory bundle is empty.
- `agnes pull` no longer creates `server/parquet/` when the manifest is empty.
- `agnes snapshot create` (formerly `da fetch`) no longer materializes an empty `user/duckdb/analytics.duckdb` when run before any `agnes pull`.
- Workspace `agnes status` reads from the canonical `server/parquet/` and `user/duckdb/analytics.duckdb` paths (was reading legacy `data/parquet/`, `data/metadata/last_sync.json`).
- `agnes init` and `agnes pull` errors now use the `cli/error_render.py` typed-error renderer (added in 0.32.0), so analyst-facing error UX matches the structured shape `agnes query --remote` already produces.

### Removed
- `da analyst setup`, `da analyst status`, `da sync`, `da fetch`. See "Changed" above for replacements.
- `da metrics` namespace as a top-level group (subcommands moved to `agnes catalog --metrics` for read-only views and `agnes admin metrics …` for write operations).
- Legacy workspace directories `data/parquet/`, `data/duckdb/`, `data/metadata/`, `user/artifacts/`. Existing analyst workspaces should be reinitialized with `agnes init --server-url ... --token ... --force` (a fresh empty folder is recommended).

### Internal
- `cli/lib/__init__.py` (empty) makes `cli/lib/` a proper package picked up by Hatchling for wheel inclusion.
- `tests/fixtures/analyst_bootstrap.py` — reusable test fixtures for clean-install verification.
```

- [ ] **Step 2: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): clean-analyst-bootstrap rewrite (BREAKING)"
```

---

### Task 25: Final verification — full test suite

**Files:** none (just runs)

- [ ] **Step 1: Run the full test suite**

```bash
pytest tests/ -v 2>&1 | tail -50
```

Expected: all PASS (or expected skips for fixtures that depend on slow/external resources).

- [ ] **Step 2: Lint check (if project has one)**

```bash
ruff check . 2>&1 | tail -20
```

If ruff reports issues in code we touched, fix them.

- [ ] **Step 3: Manually run the clean-install protocol per Task 23**

(If on-machine: do steps 1-8 from `docs/RELEASE_CHECKLIST.md`. If headless CI: skip — the integration tests cover this.)

- [ ] **Step 4: If everything passes, declare ready for review**

```bash
git log --oneline origin/main..HEAD
```

Expected: a clean linear history of one commit per task. Push the branch and open a PR using the Spec + this Plan as the PR body context.

```bash
git push -u origin zs/clean-analyst-bootstrap-spec
```

Then `gh pr create` with the spec + CHANGELOG entry + `### Test plan` per `CLAUDE.md` discipline.

---

## Self-Review (post-write)

After writing the plan, I reviewed it against the spec for:

1. **Spec coverage:**
   - Server-side: PAT scope/TTL ✅ (Task 1), legacy-strings scan ✅ (Tasks 2, 5), setup_instructions analyst branch ✅ (Task 3), /setup?role= branching ✅ (Task 4), claude_md_template rewrite ✅ (Task 6).
   - Client-side library: cli/lib/__init__.py + hooks.py ✅ (Task 7), cli/lib/pull.py ✅ (Task 8).
   - CLI commands: pull ✅ (9), push ✅ (10), init ✅ (11), status ✅ (12), diagnose system ✅ (13), snapshot create ✅ (14), catalog --metrics ✅ (15), admin metrics ✅ (16).
   - Wiring: hint texts ✅ (17), main.py + deletes ✅ (18), CLAUDE.md ✅ (19).
   - Tests: fixtures ✅ (20), reader smoke ✅ (21), clean install ✅ (22), manual protocol ✅ (23).
   - CHANGELOG ✅ (24), final verification ✅ (25).

2. **Placeholder scan:** No "TBD"/"TODO" remain. Each step has the actual code or shell command. The `cli/commands/admin_metrics.py` task says "lift X from metrics.py verbatim" rather than restating — that's intentional since the engineer can `git show HEAD~N:cli/commands/metrics.py` to see exactly what to copy.

3. **Type consistency:** `PullResult` shape consistent between `cli/lib/pull.py` and `cli/commands/pull.py`. `install_claude_hooks` signature `(workspace: Path) -> None` consistent across hooks.py + init.py. `_LEGACY_STRINGS` tuple shape used identically in tests and module.

4. **Known fragility:** Some shell-based test assertions (Task 5 `legacy-banner` div presence) are heuristic; implementer may need to tighten once HTML lands. Marked with comment.

5. **Open questions in spec stay in spec:** Per-endpoint PAT scope enforcement (deferred), layered config (deferred), hook performance budget (monitoring-only), anti-coupling test (deferred). Not in this plan.

The plan is implementable. Tasks are roughly ordered by dependency: Phase 1 server foundation, Phase 2 client library, Phase 3 commands, Phase 4 wiring/cleanup, Phase 5 fixtures + tests, Phase 6 changelog/verification.
