# Keboola Auth Provider + Provider Allowlist Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keboola OAuth web login + `X-StorageApi-Token` API auth + per-instance `auth.providers` allowlist, per `docs/superpowers/specs/2026-08-12-keboola-auth-provider-design.md`.

**Architecture:** A new `keboola` provider module follows the `google.py` pattern (authlib redirect flow → verify at the stack → shared provisioning helper → session cookie). A separate verify-client module owns all Storage-API calls (master-token gate, project binding, role gate) so the header path in `get_current_user` reuses it without OAuth dependencies. The allowlist is a small registry consulted by every provider-enumerating surface and enforced as router-level 404 dependencies.

**Tech Stack:** FastAPI, authlib (already used by Google), httpx, DuckDB/Postgres repos via factory (no new repo methods).

## Global Constraints

- Vendor-agnostic public repo: no customer names, hostnames, or project IDs anywhere in code/comments/tests. Keboola-the-platform references are fine (a connector already exists).
- Repos only via factory functions from `src.repositories` (`users_repo()`, …); never instantiate repo classes.
- No DB schema change in this feature. If any task discovers it needs a repo method, STOP — that requires the `_pg.py` sibling + contract test in the same change (spec says none are expected).
- Plaintext Keboola tokens must never appear in logs, audit rows, URLs, or argv. Only SHA-256 hashes as correlation keys.
- Every outbound verify call re-validates the target URL with `_validate_url_not_private` (import from `app.api.admin`) at use time.
- `CHANGELOG.md` bullet under `## [Unreleased]` lands in Task 8 (same PR).
- The PostToolUse hook auto-runs ruff+mypy on every edited Python file — fix what it flags before committing.
- Local testing: run ONLY the test files named in each task (CI runs the full suite on the draft PR — push after each task and let CI be the full gate).
- Stage explicit paths (`git add <paths>`), never `git add -A`.

## File Structure

```
app/keboola_identity.py                    # NEW: pure owner/project helpers (moved from admin_source_connections)
app/auth/provisioning.py                   # NEW: shared first-login provisioning (extracted from google.py)
app/auth/provider_registry.py              # NEW: auth.providers allowlist
app/auth/providers/keboola_verify.py       # NEW: /tokens/verify client + identity gates
app/auth/providers/keboola.py              # NEW: OAuth web-login provider
app/auth/keboola_header.py                 # NEW: X-StorageApi-Token cache + flood guard + resolution
app/auth/dependencies.py                   # MODIFY: header branch in get_current_user + require_session_token
app/auth/providers/{google,password,email}.py  # MODIFY: allowlist router dependency; google also refactored to provisioning helper
app/auth/router.py                         # MODIFY: POST /auth/token gated on password provider
app/auth/mcp_oauth.py                      # MODIFY: _login_url provider fallback chain
app/web/router.py                          # MODIFY: /login derivation + /login/{password,email} gating
app/web/templates/login.html               # MODIFY: keboola error messages
app/main.py                                # MODIFY: register keboola router; elevation bearer_auth
app/switches.py                            # MODIFY: keboola_token_header switch
app/api/admin.py                           # MODIFY: _URL_BEARING_FIELDS + auth.providers patch validation
app/api/admin_source_connections.py        # MODIFY: import project_identity from app.keboola_identity
config/instance.yaml.example               # MODIFY: auth.keboola + auth.providers docs; remove dead disabled_providers
docs/feature-flags.md                      # MODIFY: keboola_token_header row
tests/test_keboola_identity.py             # NEW
tests/test_auth_provisioning.py            # NEW
tests/test_auth_provider_allowlist.py      # NEW
tests/test_keboola_verify.py               # NEW
tests/test_keboola_oauth_provider.py       # NEW
tests/test_keboola_auth_header.py          # NEW
```

---

### Task 1: Shared Keboola project-identity module

**Files:**
- Create: `app/keboola_identity.py`
- Modify: `app/api/admin_source_connections.py:237-250` (replace local def with import)
- Test: `tests/test_keboola_identity.py`

**Interfaces:**
- Produces: `project_identity(payload: dict | None) -> tuple[Any | None, str]` and `project_matches(expected: Any, payload: dict | None) -> bool` in `app.keboola_identity`. Later tasks import BOTH from `app.keboola_identity`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_keboola_identity.py
"""Pure helpers for reading/binding the Keboola project identity."""

from app.keboola_identity import project_identity, project_matches


class TestProjectIdentity:
    def test_reads_owner_id_and_name(self):
        assert project_identity({"owner": {"id": 5947, "name": "Acme"}}) == (5947, "Acme")

    def test_missing_owner_id_returns_none(self):
        assert project_identity({"owner": {"name": "x"}}) == (None, "")
        assert project_identity({}) == (None, "")
        assert project_identity(None) == (None, "")


class TestProjectMatches:
    def test_int_vs_str_coercion(self):
        # config from ${ENV} interpolation is a string; verify returns int (or str).
        assert project_matches("5947", {"owner": {"id": 5947}}) is True
        assert project_matches(5947, {"owner": {"id": "5947"}}) is True

    def test_mismatch(self):
        assert project_matches("5947", {"owner": {"id": 1}}) is False

    def test_none_holes_never_match(self):
        # An unreadable identity must never compare equal (spec: explicit None reject).
        assert project_matches("5947", {}) is False
        assert project_matches(None, {"owner": {"id": 5947}}) is False
        assert project_matches(None, {}) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_keboola_identity.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.keboola_identity'`

- [ ] **Step 3: Create the module**

```python
# app/keboola_identity.py
"""Pure helpers for the Keboola project identity carried by Storage API payloads.

Shared by the admin source-connection endpoints and the Keboola auth provider,
so the int-vs-str coercion and the None-hole rejection live in exactly one
place (both bit real deployments before: Devin Review on #1242).
"""

from typing import Any, Dict, Optional


def project_identity(payload: Optional[Dict[str, Any]]) -> tuple[Optional[Any], str]:
    """``(project_id, project_name)`` from a Storage API payload that carries
    an ``owner`` block — both ``GET /tokens/verify`` and ``GET /v2/storage``
    do, so one reader serves the token preflights and the /test probe.

    Returns ``(None, "")`` when the payload has no owner id: an identity we
    cannot read must never be persisted as a *known* identity, or a
    cross-token check would compare against a hole and pass anything.
    """
    owner = (payload or {}).get("owner") or {}
    owner_id = owner.get("id")
    if owner_id is None:
        return None, ""
    return owner_id, owner.get("name") or ""


def project_matches(expected: Any, payload: Optional[Dict[str, Any]]) -> bool:
    """True iff the payload's owner id equals ``expected``.

    Compared as strings — the id round-trips through YAML/env config and JSON
    columns on two backends, so 5947 vs "5947" must not read as a mismatch.
    ``None`` on either side is an explicit reject, never a match.
    """
    if expected is None:
        return False
    project_id, _ = project_identity(payload)
    if project_id is None:
        return False
    return str(project_id) == str(expected)
```

- [ ] **Step 4: Point `admin_source_connections.py` at the shared module**

In `app/api/admin_source_connections.py`, delete the local `def project_identity(...)` (lines 237-250) and add to the import block at the top:

```python
from app.keboola_identity import project_identity
```

(The name stays importable from `app.api.admin_source_connections` for its
in-module callers; nothing else imports it today — verify with
`grep -rn "from app.api.admin_source_connections import" app/ src/ cli/ tests/`.
If any caller imports `project_identity` from there, it keeps working because
the module-level name still exists via the import.)

- [ ] **Step 5: Run tests**

Run: `.venv/bin/pytest tests/test_keboola_identity.py tests/test_admin_source_connections.py -v` (second file only if it exists — check with `ls tests/ | grep source_connection`; otherwise run `.venv/bin/pytest tests/ -k "source_connection" -q`)
Expected: PASS

- [ ] **Step 6: Commit and open the draft PR**

```bash
git add app/keboola_identity.py app/api/admin_source_connections.py tests/test_keboola_identity.py
git commit -m "refactor: extract Keboola project-identity helpers into app.keboola_identity"
git push -u origin HEAD
gh pr create --draft --title "Keboola auth provider + per-instance auth provider allowlist" --body "Implements docs/superpowers/specs/2026-08-12-keboola-auth-provider-design.md. Draft until all tasks land."
```

---

### Task 2: Shared first-login provisioning helper

**Files:**
- Create: `app/auth/provisioning.py`
- Modify: `app/auth/providers/google.py:109-148` (replace inline block with helper call)
- Test: `tests/test_auth_provisioning.py`

**Interfaces:**
- Produces: `ensure_user(email: str, name: str, *, source: str) -> dict` and `class UserDeactivatedError(Exception)` in `app.auth.provisioning`. Task 6 calls `ensure_user` from the Keboola callback.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_auth_provisioning.py
"""Contract for the shared first-login provisioning helper.

Google and Keboola logins must run the SAME four steps: create user,
Everyone membership, v39 system-plugin fanout, deactivated rejection.
"""

import pytest


@pytest.fixture
def sysdb(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-32chars-minimum!!!!!")
    from src.db import get_system_db

    conn = get_system_db()
    yield conn
    conn.close()


class TestEnsureUser:
    def test_creates_user_with_everyone_membership(self, sysdb):
        from app.auth.provisioning import ensure_user
        from src.repositories import users_repo

        user = ensure_user("new@example.com", "New User", source="test:first-signin")
        assert user["email"] == "new@example.com"
        stored = users_repo().get_by_email("new@example.com")
        assert stored is not None
        # Everyone membership (auto-membership group) was granted at creation.
        rows = sysdb.execute(
            "SELECT g.name FROM user_group_members m JOIN user_groups g ON g.id = m.group_id "
            "WHERE m.user_id = ?",
            [stored["id"]],
        ).fetchall()
        assert ("Everyone",) in rows

    def test_returning_user_is_returned_not_recreated(self, sysdb):
        from app.auth.provisioning import ensure_user

        first = ensure_user("again@example.com", "A", source="test")
        second = ensure_user("again@example.com", "A", source="test")
        assert first["id"] == second["id"]

    def test_deactivated_user_raises(self, sysdb):
        from app.auth.provisioning import UserDeactivatedError, ensure_user
        from src.repositories import users_repo

        user = ensure_user("gone@example.com", "Gone", source="test")
        users_repo().update(id=user["id"], active=False)
        with pytest.raises(UserDeactivatedError):
            ensure_user("gone@example.com", "Gone", source="test")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_auth_provisioning.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.auth.provisioning'`

(If the Everyone assertion fails for schema reasons, read how
`tests/test_auth_refresh_groups.py` asserts membership and mirror its query —
do not weaken the assertion to "user exists".)

- [ ] **Step 3: Create the helper (verbatim extraction of google.py:111-148)**

```python
# app/auth/provisioning.py
"""Shared first-login provisioning — the single write path for every auth
provider that auto-creates accounts (Google OAuth, Keboola OAuth).

Extracted verbatim from the Google callback so the four steps can never
drift apart per provider: create user → Everyone membership → v39
system-plugin fanout → deactivated-account rejection. (The Google-specific
Workspace group sync stays in google.py — it runs for returning users too
and is not provisioning.)
"""

import logging
import uuid

from src.repositories import users_repo

logger = logging.getLogger(__name__)


class UserDeactivatedError(Exception):
    """Raised when the identity maps to a deactivated Agnes account."""


def ensure_user(email: str, name: str, *, source: str) -> dict:
    """Return the user for ``email``, creating it on first login.

    ``source`` tags the Everyone-membership write (audit trail), e.g.
    ``"auth.google:first-signin"``.

    Raises :class:`UserDeactivatedError` for a deactivated account —
    callers translate that to their surface's 401/redirect.
    """
    repo = users_repo()
    user = repo.get_by_email(email)
    if not user:
        user_id = str(uuid.uuid4())
        repo.create(id=user_id, email=email, name=name)
        # Issue #748: auto-grant Everyone at creation (source='system_seed')
        # unless AGNES_GROUP_EVERYONE_EMAIL maps Everyone to a Workspace
        # group. Creation-time only: never called again for a returning
        # user, so an admin's manual removal later sticks.
        try:
            from app.auth.group_sync import ensure_everyone_membership

            ensure_everyone_membership(user_id, added_by=source)
        except Exception:
            logger.exception("ensure_everyone_membership failed for new user %s", email)
        # v39: subscribe new user to every system plugin so the mandatory
        # tier reaches them on their first session without an admin
        # reconcile. Fail-soft.
        try:
            from src.repositories import user_curated_subscriptions_repo

            user_curated_subscriptions_repo().fanout_system_for_user(user_id)
        except Exception:
            logger.exception("system-plugin fanout failed for new user %s", email)
        user = repo.get_by_email(email)
    if not bool(user.get("active", True)):
        raise UserDeactivatedError(email)
    return user
```

- [ ] **Step 4: Refactor the Google callback to use it**

In `app/auth/providers/google.py`, replace lines 111-148 (from `repo = users_repo()` through the `return RedirectResponse(url="/login?error=deactivated")` block) with:

```python
            from app.auth.provisioning import UserDeactivatedError, ensure_user

            try:
                user = ensure_user(email, name, source="auth.google:first-signin")
            except UserDeactivatedError:
                return RedirectResponse(url="/login?error=deactivated")
```

Keep everything around it unchanged: the `conn = None if use_pg() else get_system_db()` block stays (still needed by `apply_user_groups(user["id"], email, conn)` below), and the `sync_result` / `denied` logic stays verbatim. Remove the now-unused `import uuid` and `from src.repositories import users_repo` if nothing else in the file uses them (check before deleting — `users_repo` may have other callers in the module).

- [ ] **Step 5: Run tests**

Run: `.venv/bin/pytest tests/test_auth_provisioning.py tests/test_auth_providers.py tests/test_auth_refresh_groups.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/auth/provisioning.py app/auth/providers/google.py tests/test_auth_provisioning.py
git commit -m "refactor: extract shared first-login provisioning from the Google callback"
git push
```

---

### Task 3: Provider allowlist — registry + enforcement on every surface

**Files:**
- Create: `app/auth/provider_registry.py`
- Modify: `app/auth/providers/google.py:27`, `app/auth/providers/password.py:38`, `app/auth/providers/email.py:34` (router dependencies)
- Modify: `app/auth/router.py` (`POST /auth/token` gate, endpoint at lines 70-117)
- Modify: `app/web/router.py:959-1002` (login derivation), `:1005-1036` (sub-pages)
- Modify: `app/auth/mcp_oauth.py:650-667` (`_login_url`)
- Modify: `app/api/admin.py` (patch validation), `config/instance.yaml.example:229-230` (remove dead key)
- Test: `tests/test_auth_provider_allowlist.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: in `app.auth.provider_registry`: `KNOWN_PROVIDERS: tuple[str, ...]`, `configured_allowlist() -> list[str] | None`, `provider_allowed(name: str) -> bool`, `require_provider(name: str) -> Callable[[], None]` (FastAPI dependency factory raising 404). Task 6 reuses `require_provider("keboola")` and `provider_allowed`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_auth_provider_allowlist.py
"""auth.providers allowlist: unset = today's behavior; set = allowlist ∩ availability;
excluded providers' endpoints 404 — including the shared-router POST /auth/token."""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def make_client(tmp_path, monkeypatch):
    def _make(providers_env: str | None):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-32chars-minimum!!!!!")
        if providers_env is None:
            monkeypatch.delenv("AGNES_AUTH_PROVIDERS", raising=False)
        else:
            monkeypatch.setenv("AGNES_AUTH_PROVIDERS", providers_env)
        from app.main import create_app

        return TestClient(create_app())

    return _make


class TestRegistry:
    def test_unset_allows_everything(self, monkeypatch):
        monkeypatch.delenv("AGNES_AUTH_PROVIDERS", raising=False)
        from app.auth.provider_registry import provider_allowed

        assert all(provider_allowed(p) for p in ("google", "email", "password", "keboola"))

    def test_set_narrows(self, monkeypatch):
        monkeypatch.setenv("AGNES_AUTH_PROVIDERS", "google")
        from app.auth.provider_registry import provider_allowed

        assert provider_allowed("google") is True
        assert provider_allowed("password") is False

    def test_unknown_names_ignored_empty_result_treated_as_unset(self, monkeypatch):
        monkeypatch.setenv("AGNES_AUTH_PROVIDERS", "definitely-not-a-provider")
        from app.auth.provider_registry import configured_allowlist

        assert configured_allowlist() is None  # fail-open, loudly logged


class TestEndpointGating:
    def test_password_endpoints_404_when_excluded(self, make_client):
        client = make_client("google")
        # Router-level dependency: any matched route under /auth/password 404s.
        # (POST — the login form route; a GET would 405 before dependencies run.)
        assert client.post("/auth/password/login/web", data={}).status_code == 404
        # The easy-to-miss one: the shared-router password grant.
        resp = client.post("/auth/token", data={"email": "a@b.c", "password": "x"})
        assert resp.status_code == 404
        # Login sub-page is gated too.
        assert client.get("/login/password").status_code == 404

    def test_password_endpoints_live_when_unset(self, make_client):
        client = make_client(None)
        resp = client.post("/auth/token", data={"email": "nobody@example.com", "password": "x"})
        assert resp.status_code != 404  # 401/422 is fine — the endpoint exists

    def test_login_page_hides_excluded_buttons(self, make_client):
        client = make_client("email")
        html = client.get("/login").text
        assert "Sign in with Email Link" in html
        assert "Sign in with Email &amp; Password" not in html and "Sign in with Email & Password" not in html

    def test_login_page_unset_is_todays_behavior(self, make_client):
        client = make_client(None)
        html = client.get("/login").text
        # No Google credentials in the test env → password + email link, exactly as before.
        assert "Sign in with Email & Password" in html or "Sign in with Email &amp; Password" in html
        assert "Sign in with Email Link" in html
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_auth_provider_allowlist.py -v`
Expected: FAIL (`No module named 'app.auth.provider_registry'`)

Note: `TestEndpointGating` builds several apps in one session — if a fixture
collision appears (DuckDB single-writer), split `make_client` calls across
test functions exactly as written above (one app per test), which avoids it.

- [ ] **Step 3: Create the registry**

```python
# app/auth/provider_registry.py
"""Per-instance auth provider allowlist (spec 2026-08-12).

``auth.providers`` in instance.yaml (env override ``AGNES_AUTH_PROVIDERS``,
comma-separated) narrows which login methods this instance offers. Unset =
every available provider — byte-for-byte the pre-allowlist behavior. An
explicitly empty (or all-unknown) list is a misconfiguration: rejected at
the admin API, and treated here as unset with a loud error log so one
overlay write can never lock every user out of the instance.
"""

import logging
import os
from typing import Callable, Optional

from fastapi import HTTPException

from app.instance_config import get_value

logger = logging.getLogger(__name__)

KNOWN_PROVIDERS: tuple[str, ...] = ("google", "email", "password", "keboola")


def configured_allowlist() -> Optional[list[str]]:
    raw_env = os.environ.get("AGNES_AUTH_PROVIDERS")
    if raw_env is not None:
        values = [v.strip() for v in raw_env.split(",") if v.strip()]
    else:
        configured = get_value("auth", "providers")
        if configured is None:
            return None
        if isinstance(configured, str):
            values = [v.strip() for v in configured.split(",") if v.strip()]
        else:
            values = [str(v).strip() for v in configured if str(v).strip()]
    unknown = [v for v in values if v not in KNOWN_PROVIDERS]
    for name in unknown:
        logger.warning("auth.providers: unknown provider %r ignored", name)
    known = [v for v in values if v in KNOWN_PROVIDERS]
    if not known:
        logger.error(
            "auth.providers is set but names no known provider — treating as unset "
            "(all providers) so the instance stays reachable; fix the configuration"
        )
        return None
    return known


def provider_allowed(name: str) -> bool:
    allowlist = configured_allowlist()
    return allowlist is None or name in allowlist


def require_provider(name: str) -> Callable[[], None]:
    """Router-level dependency: excluded provider endpoints return 404
    (not 403 — an excluded method should not advertise its existence)."""

    def _dep() -> None:
        if not provider_allowed(name):
            raise HTTPException(status_code=404, detail="Not Found")

    return _dep
```

- [ ] **Step 4: Enforce on the provider routers**

In each of `google.py`, `password.py`, `email.py`, change the router constructor (google shown; same shape for the others with their name):

```python
from fastapi import Depends

from app.auth.provider_registry import require_provider

router = APIRouter(
    prefix="/auth/google",
    tags=["auth"],
    dependencies=[Depends(require_provider("google"))],
)
```

In `app/auth/router.py`, inside the `POST /auth/token` handler (the password
grant at lines 70-117), add as the FIRST lines of the body:

```python
    from app.auth.provider_registry import provider_allowed

    if not provider_allowed("password"):
        raise HTTPException(status_code=404, detail="Not Found")
```

- [ ] **Step 5: Enforce on the web login surfaces**

In `app/web/router.py` login derivation (lines 959-974), gate every append:

```python
    from app.auth.provider_registry import provider_allowed

    providers = []
    try:
        from app.auth.providers.google import is_available as google_available

        if google_available() and provider_allowed("google"):
            providers.append({"name": "google", "display_name": "Google", "icon": "google"})
    except Exception:
        pass
    if provider_allowed("password"):
        providers.append({"name": "password", "display_name": "Email & Password", "icon": "key"})
    try:
        from app.auth.providers.email import is_available as email_available

        if email_available() and provider_allowed("email"):
            providers.append({"name": "email", "display_name": "Email Link", "icon": "mail"})
    except Exception:
        pass
```

In `login_password_page` (line 1005) and `login_email_page` (line 1022): add a
404 guard at the top of each (`password` / `email` respectively):

```python
    from app.auth.provider_registry import provider_allowed

    if not provider_allowed("password"):  # "email" in login_email_page
        raise HTTPException(status_code=404, detail="Not Found")
```

(`HTTPException` is already imported in `app/web/router.py`; verify, add if not.)
And in both pages gate the Google cross-link: `google_ok = google_available() and provider_allowed("google")`.

In `app/auth/mcp_oauth.py::_login_url` (lines 658-664), replace the
availability check:

```python
    from app.auth.provider_registry import provider_allowed
    from app.auth.providers.google import is_available as google_available

    if google_available() and provider_allowed("google"):
        ...unchanged google branch...
```

(Task 6 adds the keboola fallback branch here — leave a plain `/login` fall-through for now.)

- [ ] **Step 6: Admin API validation + example cleanup**

In `app/api/admin.py`, next to `_validate_urls_in_patch` (line 226), add:

```python
def _validate_auth_providers_in_patch(sections: Dict[str, Dict[str, Any]]) -> None:
    """Reject an explicitly empty auth.providers — one overlay write must
    never be able to lock every user out (spec: empty list is a config error)."""
    auth = sections.get("auth")
    if not isinstance(auth, dict) or "providers" not in auth:
        return
    value = auth["providers"]
    if value is None:
        return
    if not isinstance(value, list) or not value:
        raise HTTPException(
            status_code=422,
            detail="auth.providers must be a non-empty list of provider names (or omitted entirely)",
        )
```

Find the call site of `_validate_urls_in_patch(` in the server-config POST
handler (`grep -n "_validate_urls_in_patch(" app/api/admin.py`) and call
`_validate_auth_providers_in_patch(sections)` on the next line.

In `config/instance.yaml.example`, delete lines 229-230 (the dead
`disabled_providers` block — nothing consumes it) and in its place document
the real key:

```yaml
  # providers: [google, email, password, keboola]
  #                                 # Which login methods this instance offers.
  #                                 # Unset = every configured provider (default).
  #                                 # Explicit empty list is rejected. Excluded
  #                                 # providers' endpoints return 404. Env
  #                                 # override: AGNES_AUTH_PROVIDERS (comma-sep).
```

- [ ] **Step 7: Run tests**

Run: `.venv/bin/pytest tests/test_auth_provider_allowlist.py tests/test_auth_providers.py tests/test_admin_configure_api.py -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add app/auth/provider_registry.py app/auth/providers/google.py app/auth/providers/password.py app/auth/providers/email.py app/auth/router.py app/web/router.py app/auth/mcp_oauth.py app/api/admin.py config/instance.yaml.example tests/test_auth_provider_allowlist.py
git commit -m "feat: per-instance auth provider allowlist (auth.providers)"
git push
```

---

### Task 4: Keboola config surface — switch, SSRF fields, example, feature-flags doc

**Files:**
- Modify: `app/switches.py` (append to `SWITCHES` after `mcp_query_param_token`, line ~242)
- Modify: `app/api/admin.py:220-223` (`_URL_BEARING_FIELDS`)
- Modify: `config/instance.yaml.example` (auth.keboola block)
- Modify: `docs/feature-flags.md` (row for the new switch)
- Test: existing `tests/test_switches.py` + `tests/test_admin_configure_api.py` sweeps

**Interfaces:**
- Produces: switch name `keboola_token_header`, read via `from app.switches import switch_value; switch_value("keboola_token_header")` → bool, default `False`. Task 7 consumes it.

- [ ] **Step 1: Add the switch**

In `app/switches.py`, append to `SWITCHES` (after the `mcp_source_url_strict` entry):

```python
    Switch(
        name="keboola_token_header",
        config_keys=("auth", "keboola", "allow_token_header"),
        env_var="AGNES_KEBOOLA_ALLOW_TOKEN_HEADER",
        kind="bool",
        default=False,
        effect="live",
        category="operations",
        editable=True,
        description=(
            "Accept a Keboola Storage API token in the X-StorageApi-Token header as API "
            "authentication. The token is verified against the configured stack per request "
            "(60s cache), must be a master token for the bound project, and maps only to an "
            "EXISTING user — it never provisions accounts. Off by default: a plain Storage "
            "token carries no interactive factor, so enabling this bypasses any MFA/SSO the "
            "organization enforces on web logins."
        ),
    ),
```

- [ ] **Step 2: Register the URL-bearing fields**

In `app/api/admin.py`, extend `_URL_BEARING_FIELDS`:

```python
_URL_BEARING_FIELDS: tuple[tuple[str, ...], ...] = (
    ("data_source", "keboola", "stack_url"),
    ("marketplace", "curators_url"),
    ("auth", "keboola", "stack_url"),
    ("auth", "keboola", "oauth_host"),
)
```

- [ ] **Step 3: Document the config block**

In `config/instance.yaml.example`, after the `providers:` block added in Task 3:

```yaml
  # --- Keboola login (optional) ---
  # Lets users sign in with their Keboola platform identity. The OAuth
  # client (client_id/client_secret) is issued by Keboola for your stack —
  # ask your Keboola contact; it is not self-service. The token-header API
  # auth (auth.keboola.allow_token_header switch) works without any OAuth
  # client. Membership in project_id is the trust boundary: ANYONE that
  # project admits (including guest/readOnly collaborators) can sign in and
  # self-provision unless allowed_roles narrows it.
  # keboola:
  #   client_id: "${KEBOOLA_OAUTH_CLIENT_ID}"
  #   client_secret: "${KEBOOLA_OAUTH_CLIENT_SECRET}"
  #   stack_url: ""                 # default: data_source.keboola.stack_url
  #   oauth_host: ""                # default: stack_url (OAuth lives on the connection host)
  #   project_id: ""                # REQUIRED — tokens from other projects are rejected
  #   allowed_roles: []             # e.g. [admin, share]; empty/unset = any project role
  #   allow_token_header: false     # X-StorageApi-Token API auth; see docs/feature-flags.md
```

- [ ] **Step 4: Add the feature-flags row**

Read the per-flag section format in `docs/feature-flags.md` (it lists each flag
with name / keys / env / default) and append `keboola_token_header` following
that exact format, with keys `auth.keboola.allow_token_header`, env
`AGNES_KEBOOLA_ALLOW_TOKEN_HEADER`, default `false`, and the MFA-bypass warning
sentence from the switch description.

- [ ] **Step 5: Run the registry sweeps**

Run: `.venv/bin/pytest tests/test_switches.py tests/test_admin_configure_api.py -v`
Expected: PASS (the sweeps discover the new switch mechanically; failures name
exactly what's missing — fix per their message)

- [ ] **Step 6: Commit**

```bash
git add app/switches.py app/api/admin.py config/instance.yaml.example docs/feature-flags.md
git commit -m "feat: keboola_token_header switch + SSRF validation for auth.keboola URLs"
git push
```

---

### Task 5: Keboola verify client (`/tokens/verify` + identity gates)

**Files:**
- Create: `app/auth/providers/keboola_verify.py`
- Test: `tests/test_keboola_verify.py`

**Interfaces:**
- Consumes: `project_matches`, `project_identity` from `app.keboola_identity` (Task 1).
- Produces (Task 6 + Task 7 import the MODULE — `from app.auth.providers import keboola_verify` — and call through it, so tests can monkeypatch):
  - `class KeboolaVerifyError(Exception)` with `.reason: str` in `{"not_configured","verify_failed","invalid_token","not_master_token","no_admin_identity","project_mismatch","role_forbidden"}` and `.detail: str`
  - `@dataclass(frozen=True) VerifiedKeboolaIdentity(token_id, project_id, project_name, email, name, role)` — all `str`
  - `stack_url() -> str | None`, `oauth_host() -> str | None`, `configured_project_id() -> str | None`, `allowed_roles() -> list[str] | None`, `client_id() -> str`, `client_secret() -> str`
  - `verify_storage_token(token: str) -> VerifiedKeboolaIdentity` (X-StorageApi-Token header)
  - `verify_oauth_access_token(access_token: str) -> VerifiedKeboolaIdentity` (Authorization: Bearer)
  - `_fetch_verify(base_url: str, headers: dict) -> dict` — the ONLY function that talks HTTP; tests monkeypatch it

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_keboola_verify.py
"""Identity gates over the /tokens/verify payload — master-token gate,
project binding, role gate, defensive adminOwner handling."""

import pytest

from app.auth.providers import keboola_verify as kv


def _payload(**overrides):
    base = {
        "id": "204",
        "isMasterToken": True,
        "owner": {"id": 5947, "name": "Acme DWH"},
        "admin": {"id": 42, "name": "Jane", "role": "admin"},
        "adminOwner": {"id": 42, "email": "jane@example.com", "name": "Jane"},
    }
    base.update(overrides)
    return base


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(kv, "stack_url", lambda: "https://connection.example.com")
    monkeypatch.setattr(kv, "configured_project_id", lambda: "5947")
    monkeypatch.setattr(kv, "allowed_roles", lambda: None)


class TestGates:
    def test_happy_path(self, configured, monkeypatch):
        monkeypatch.setattr(kv, "_fetch_verify", lambda url, headers: _payload())
        identity = kv.verify_storage_token("tok")
        assert identity.email == "jane@example.com"
        assert identity.project_id == "5947"
        assert identity.role == "admin"

    def test_non_master_token_rejected_even_with_adminowner(self, configured, monkeypatch):
        # The escalation case: a restricted token created by an admin verifies
        # WITH a back-filled adminOwner. isMasterToken is the discriminator.
        monkeypatch.setattr(kv, "_fetch_verify", lambda url, headers: _payload(isMasterToken=False))
        with pytest.raises(kv.KeboolaVerifyError) as exc:
            kv.verify_storage_token("tok")
        assert exc.value.reason == "not_master_token"

    def test_project_mismatch(self, configured, monkeypatch):
        monkeypatch.setattr(kv, "_fetch_verify", lambda url, headers: _payload(owner={"id": 1, "name": "Other"}))
        with pytest.raises(kv.KeboolaVerifyError) as exc:
            kv.verify_storage_token("tok")
        assert exc.value.reason == "project_mismatch"

    def test_missing_owner_id_is_mismatch_not_pass(self, configured, monkeypatch):
        monkeypatch.setattr(kv, "_fetch_verify", lambda url, headers: _payload(owner={}))
        with pytest.raises(kv.KeboolaVerifyError) as exc:
            kv.verify_storage_token("tok")
        assert exc.value.reason == "project_mismatch"

    def test_missing_adminowner_email_is_explicit_failure(self, configured, monkeypatch):
        monkeypatch.setattr(kv, "_fetch_verify", lambda url, headers: _payload(adminOwner={}))
        with pytest.raises(kv.KeboolaVerifyError) as exc:
            kv.verify_storage_token("tok")
        assert exc.value.reason == "no_admin_identity"

    def test_role_gate(self, configured, monkeypatch):
        monkeypatch.setattr(kv, "allowed_roles", lambda: ["admin", "share"])
        monkeypatch.setattr(
            kv, "_fetch_verify",
            lambda url, headers: _payload(admin={"id": 42, "name": "J", "role": "readOnly"}),
        )
        with pytest.raises(kv.KeboolaVerifyError) as exc:
            kv.verify_storage_token("tok")
        assert exc.value.reason == "role_forbidden"

    def test_unconfigured_stack_fails_closed(self, monkeypatch):
        monkeypatch.setattr(kv, "stack_url", lambda: None)
        with pytest.raises(kv.KeboolaVerifyError) as exc:
            kv.verify_storage_token("tok")
        assert exc.value.reason == "not_configured"

    def test_headers_choose_auth_scheme(self, configured, monkeypatch):
        seen = {}

        def fake(url, headers):
            seen.update(headers)
            return _payload()

        monkeypatch.setattr(kv, "_fetch_verify", fake)
        kv.verify_storage_token("plain-tok")
        assert seen == {"X-StorageApi-Token": "plain-tok"}
        seen.clear()
        kv.verify_oauth_access_token("oauth-tok")
        assert seen == {"Authorization": "Bearer oauth-tok"}
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_keboola_verify.py -v`
Expected: FAIL (`No module named 'app.auth.providers.keboola_verify'`)

- [ ] **Step 3: Implement the module**

```python
# app/auth/providers/keboola_verify.py
"""Keboola Storage API token verification for the auth provider.

One module owns every Storage-API identity decision (master-token gate,
project binding, role gate) so the OAuth callback and the
X-StorageApi-Token header path can never drift apart. HTTP goes through
``_fetch_verify`` exclusively — tests monkeypatch it; callers import THIS
MODULE (not names from it) for the same reason.

Facts this encodes (verified against the platform, 2026-08-12):
- ``adminOwner`` is back-filled through the token's creator chain, so its
  presence does NOT mean "admin token" — a restricted bucket token created
  by an admin carries the admin's identity. ``isMasterToken`` is the
  discriminator; gating on adminOwner would let any holder of a scoped
  service token authenticate as the human who created it.
- ``adminOwner`` on the verify response is real but publicly undocumented —
  handle absence defensively, never crash.
"""

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

import httpx

from app.instance_config import get_value
from app.keboola_identity import project_identity, project_matches

logger = logging.getLogger(__name__)

VERIFY_TIMEOUT_SECONDS = 5.0


class KeboolaVerifyError(Exception):
    """A verify/gate failure. ``reason`` is machine-readable; ``detail`` is
    the operator/user-facing sentence. The token itself must never appear
    in either."""

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        self.detail = detail or reason
        super().__init__(self.detail)


@dataclass(frozen=True)
class VerifiedKeboolaIdentity:
    token_id: str
    project_id: str
    project_name: str
    email: str
    name: str
    role: str


def stack_url() -> Optional[str]:
    url = get_value("auth", "keboola", "stack_url") or get_value("data_source", "keboola", "stack_url")
    return str(url).rstrip("/") if url else None


def oauth_host() -> Optional[str]:
    url = get_value("auth", "keboola", "oauth_host")
    return str(url).rstrip("/") if url else stack_url()


def configured_project_id() -> Optional[str]:
    value = get_value("auth", "keboola", "project_id")
    if value is None or str(value).strip() == "":
        return None
    return str(value).strip()


def allowed_roles() -> Optional[list[str]]:
    value = get_value("auth", "keboola", "allowed_roles")
    if not value:
        return None
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return [str(v).strip() for v in value if str(v).strip()]


def client_id() -> str:
    return str(get_value("auth", "keboola", "client_id") or "")


def client_secret() -> str:
    return str(get_value("auth", "keboola", "client_secret") or "")


def _fetch_verify(base_url: str, headers: Dict[str, str]) -> Dict[str, Any]:
    """GET {base_url}/v2/storage/tokens/verify. The ONLY HTTP call site.

    SSRF: the target is re-validated at every use (not store time) — same
    DNS-rebind / metadata-endpoint posture as the admin source-connection
    verify calls.
    """
    from app.api.admin import _validate_url_not_private

    _validate_url_not_private(base_url, "auth.keboola.stack_url")
    try:
        resp = httpx.get(
            f"{base_url}/v2/storage/tokens/verify",
            headers=headers,
            timeout=VERIFY_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        logger.warning("Keboola verify call failed: %s", type(exc).__name__)
        raise KeboolaVerifyError("verify_failed", "Could not reach the Keboola stack to verify the token")
    if resp.status_code in (400, 401, 403):
        raise KeboolaVerifyError("invalid_token", "Keboola rejected the token")
    if resp.status_code != 200:
        logger.warning("Keboola verify returned HTTP %s", resp.status_code)
        raise KeboolaVerifyError("verify_failed", f"Keboola verify returned HTTP {resp.status_code}")
    return resp.json()


def _identity_from_payload(payload: Dict[str, Any]) -> VerifiedKeboolaIdentity:
    if not payload.get("isMasterToken"):
        raise KeboolaVerifyError(
            "not_master_token",
            "Only a master (admin) Storage API token can authenticate — restricted tokens are rejected",
        )
    expected = configured_project_id()
    if expected is None:
        raise KeboolaVerifyError("not_configured", "auth.keboola.project_id is not configured")
    if not project_matches(expected, payload):
        raise KeboolaVerifyError(
            "project_mismatch",
            f"The token belongs to a different Keboola project than this instance is bound to (expected project {expected})",
        )
    admin_owner = payload.get("adminOwner") or {}
    email = str(admin_owner.get("email") or "").strip()
    if not email:
        raise KeboolaVerifyError(
            "no_admin_identity",
            "The verified token carries no admin identity (adminOwner.email missing)",
        )
    role = str((payload.get("admin") or {}).get("role") or "")
    roles = allowed_roles()
    if roles is not None and role not in roles:
        raise KeboolaVerifyError(
            "role_forbidden",
            f"Keboola project role {role or 'unknown'!r} is not permitted on this instance",
        )
    project_id, project_name = project_identity(payload)
    return VerifiedKeboolaIdentity(
        token_id=str(payload.get("id") or ""),
        project_id=str(project_id),
        project_name=project_name,
        email=email,
        name=str(admin_owner.get("name") or ""),
        role=role,
    )


def verify_storage_token(token: str) -> VerifiedKeboolaIdentity:
    """Verify a plain Storage API token (X-StorageApi-Token header path)."""
    base = stack_url()
    if not base:
        raise KeboolaVerifyError("not_configured", "No Keboola stack URL configured")
    payload = _fetch_verify(base, {"X-StorageApi-Token": token})
    return _identity_from_payload(payload)


def verify_oauth_access_token(access_token: str) -> VerifiedKeboolaIdentity:
    """Verify an OAuth access token from the login flow (Bearer path).

    Named assumption (spec): Bearer acceptance on /tokens/verify is real but
    publicly undocumented platform behavior.
    """
    base = stack_url()
    if not base:
        raise KeboolaVerifyError("not_configured", "No Keboola stack URL configured")
    payload = _fetch_verify(base, {"Authorization": f"Bearer {access_token}"})
    return _identity_from_payload(payload)
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_keboola_verify.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/auth/providers/keboola_verify.py tests/test_keboola_verify.py
git commit -m "feat: Keboola /tokens/verify client with master-token, project and role gates"
git push
```

---

### Task 6: Keboola OAuth web-login provider

**Files:**
- Create: `app/auth/providers/keboola.py`
- Modify: `app/main.py` (~line 2392, register router next to the other providers)
- Modify: `app/web/router.py` (login derivation: keboola button; error context)
- Modify: `app/web/templates/login.html:158-166` (error messages)
- Modify: `app/auth/mcp_oauth.py::_login_url` (keboola fallback)
- Test: `tests/test_keboola_oauth_provider.py`

**Interfaces:**
- Consumes: `keboola_verify` module (Task 5), `ensure_user`/`UserDeactivatedError` (Task 2), `require_provider`/`provider_allowed` (Task 3).
- Produces: routes `GET /auth/keboola/login`, `GET /auth/keboola/callback` (route name `keboola_callback`); `is_available() -> bool` in `app.auth.providers.keboola`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_keboola_oauth_provider.py
"""Keboola OAuth web login: availability, redirect, callback outcomes."""

import pytest
from fastapi.testclient import TestClient

from app.auth.providers import keboola_verify as kv


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-32chars-minimum!!!!!")
    monkeypatch.setattr(kv, "stack_url", lambda: "https://connection.example.com")
    monkeypatch.setattr(kv, "configured_project_id", lambda: "5947")
    monkeypatch.setattr(kv, "client_id", lambda: "cid")
    monkeypatch.setattr(kv, "client_secret", lambda: "csecret")
    from app.main import create_app

    return TestClient(create_app())


def _identity(email="jane@example.com"):
    return kv.VerifiedKeboolaIdentity(
        token_id="204", project_id="5947", project_name="Acme DWH",
        email=email, name="Jane", role="admin",
    )


class TestLoginRoute:
    def test_redirects_to_oauth_host(self, client):
        resp = client.get("/auth/keboola/login", follow_redirects=False)
        assert resp.status_code in (302, 307)
        assert resp.headers["location"].startswith("https://connection.example.com/oauth/authorize")

    def test_unconfigured_redirects_to_login_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-32chars-minimum!!!!!")
        monkeypatch.setattr(kv, "client_id", lambda: "")
        from app.main import create_app

        c = TestClient(create_app())
        resp = c.get("/auth/keboola/login", follow_redirects=False)
        assert resp.status_code == 307 or resp.status_code == 302
        assert "error=keboola_not_configured" in resp.headers["location"]


class TestCallback:
    def _patch_flow(self, monkeypatch, identity=None, verify_error=None):
        from app.auth.providers import keboola as kb

        async def fake_authorize_access_token(request):
            return {"access_token": "at-123"}

        class FakeApp:
            authorize_access_token = staticmethod(fake_authorize_access_token)

        monkeypatch.setattr(kb, "_oauth_client", lambda: FakeApp())
        if verify_error is not None:
            def boom(tok):
                raise verify_error

            monkeypatch.setattr(kv, "verify_oauth_access_token", boom)
        else:
            monkeypatch.setattr(kv, "verify_oauth_access_token", lambda tok: identity or _identity())

    def test_happy_path_provisions_and_sets_cookie(self, client, monkeypatch):
        self._patch_flow(monkeypatch)
        resp = client.get("/auth/keboola/callback?code=x&state=y", follow_redirects=False)
        assert resp.status_code == 302
        assert "access_token" in resp.cookies
        from src.repositories import users_repo

        assert users_repo().get_by_email("jane@example.com") is not None

    def test_project_mismatch_redirects_with_error(self, client, monkeypatch):
        self._patch_flow(monkeypatch, verify_error=kv.KeboolaVerifyError("project_mismatch", "wrong project"))
        resp = client.get("/auth/keboola/callback?code=x&state=y", follow_redirects=False)
        assert resp.status_code == 302
        assert "error=keboola_project_mismatch" in resp.headers["location"]
        assert "access_token" not in resp.cookies

    def test_deactivated_user_rejected(self, client, monkeypatch):
        from src.repositories import users_repo
        import uuid

        uid = str(uuid.uuid4())
        users_repo().create(id=uid, email="jane@example.com", name="Jane")
        users_repo().update(id=uid, active=False)
        self._patch_flow(monkeypatch)
        resp = client.get("/auth/keboola/callback?code=x&state=y", follow_redirects=False)
        assert resp.status_code == 302
        assert "error=deactivated" in resp.headers["location"]
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_keboola_oauth_provider.py -v`
Expected: FAIL (`No module named 'app.auth.providers.keboola'` — the 404 comes
from the unregistered router)

- [ ] **Step 3: Implement the provider**

```python
# app/auth/providers/keboola.py
"""Keboola OAuth login provider.

Same shape as the Google provider: authlib redirect flow with session-backed
``state``, then a session cookie. Identity comes from verifying the OAuth
access token against the stack's /tokens/verify (see keboola_verify — the
master-token/project/role gates live there). First login auto-provisions via
the shared helper; membership in the configured project is the trust
boundary, so the allowed_domain filter is deliberately NOT applied here.
"""

import logging

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool

from app.auth._common import safe_next_path
from app.auth.jwt import SESSION_COOKIE_MAX_AGE_SECONDS, create_access_token
from app.auth.provider_registry import require_provider
from app.auth.providers import keboola_verify as kv
from app.auth.provisioning import UserDeactivatedError, ensure_user

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/auth/keboola",
    tags=["auth"],
    dependencies=[Depends(require_provider("keboola"))],
)

oauth = OAuth()

# KeboolaVerifyError.reason → /login?error=<code>. Every code has copy in
# login.html; anything unmapped falls back to the generic failure.
_ERROR_CODE_BY_REASON = {
    "project_mismatch": "keboola_project_mismatch",
    "not_master_token": "keboola_not_permitted",
    "role_forbidden": "keboola_not_permitted",
    "no_admin_identity": "keboola_not_permitted",
    "invalid_token": "keboola_oauth_failed",
    "verify_failed": "keboola_oauth_failed",
    "not_configured": "keboola_not_configured",
}


def is_available() -> bool:
    """Config-completeness only — the allowlist is a separate layer (spec)."""
    return bool(kv.client_id() and kv.client_secret() and kv.configured_project_id() and kv.stack_url())


def _oauth_client():
    """Lazily register the authlib client (config is instance.yaml, read at
    first use, unlike Google's import-time env vars). Safe to call repeatedly."""
    client = oauth.create_client("keboola")
    if client is not None:
        return client
    host = kv.oauth_host()
    oauth.register(
        name="keboola",
        client_id=kv.client_id(),
        client_secret=kv.client_secret(),
        authorize_url=f"{host}/oauth/authorize",
        access_token_url=f"{host}/oauth/token",
        client_kwargs={"scope": "email"},
    )
    return oauth.create_client("keboola")


@router.get("/login")
async def keboola_login(request: Request):
    """Redirect to the Keboola OAuth authorize endpoint (state in session)."""
    if not is_available():
        return RedirectResponse(url="/login?error=keboola_not_configured")
    next_path = safe_next_path(request.query_params.get("next"), default="")
    if next_path:
        request.session["login_next"] = next_path
    else:
        request.session.pop("login_next", None)
    redirect_uri = str(request.url_for("keboola_callback"))
    return await _oauth_client().authorize_redirect(request, redirect_uri)


@router.get("/callback")
async def keboola_callback(request: Request):
    """Exchange the code, verify the access token at the stack, sign in."""
    if not is_available():
        return RedirectResponse(url="/login?error=keboola_not_configured")
    try:
        token = await _oauth_client().authorize_access_token(request)
    except Exception:
        logger.exception("Keboola OAuth token exchange failed")
        return RedirectResponse(url="/login?error=keboola_oauth_failed")
    access_token = str(token.get("access_token") or "")
    try:
        # Sync HTTP verify off the event loop (same Tier-1 posture as auth).
        identity = await run_in_threadpool(kv.verify_oauth_access_token, access_token)
    except kv.KeboolaVerifyError as exc:
        logger.info("Keboola login rejected: %s", exc.reason)
        code = _ERROR_CODE_BY_REASON.get(exc.reason, "keboola_oauth_failed")
        return RedirectResponse(url=f"/login?error={code}")
    try:
        user = await run_in_threadpool(
            ensure_user, identity.email, identity.name, source="auth.keboola:first-signin"
        )
    except UserDeactivatedError:
        return RedirectResponse(url="/login?error=deactivated")

    jwt_token = create_access_token(user["id"], user["email"])
    target = safe_next_path(request.session.pop("login_next", None))

    from app.auth.public_url import cookie_secure
    from app.instance_config import session_cookie_domain

    response = RedirectResponse(url=target, status_code=302)
    response.set_cookie(
        key="access_token",
        value=jwt_token,
        httponly=True,
        max_age=SESSION_COOKIE_MAX_AGE_SECONDS,
        samesite="lax",
        secure=cookie_secure(request),
        domain=session_cookie_domain(),
    )
    return response
```

- [ ] **Step 4: Register the router**

In `app/main.py`, next to the other provider imports (~line 2392):

```python
    from app.auth.providers.keboola import router as keboola_auth_router
```

and after `app.include_router(email_auth_router)`:

```python
    app.include_router(keboola_auth_router)  # Always register, availability + allowlist per-request
```

- [ ] **Step 5: Login page button + error copy**

In `app/web/router.py` login derivation (extend the Task-3 block, after the
email append):

```python
    try:
        from app.auth.providers.keboola import is_available as keboola_available

        if keboola_available() and provider_allowed("keboola"):
            providers.append({"name": "keboola", "display_name": "Keboola", "icon": "keboola"})
    except Exception:
        pass
```

and in the `login_buttons` loop:

```python
        elif p["name"] == "keboola":
            _url = "/auth/keboola/login"
            if next_path:
                _url += f"?next={quote(next_path, safe='')}"
            login_buttons.append(
                {"url": _url, "text": "Sign in with Keboola", "css_class": "btn-primary", "icon_html": ""}
            )
```

Add the expected-project context for the mismatch page: in the same handler,
right before `ctx = _build_context(...)`:

```python
    keboola_expected_project = ""
    if request.query_params.get("error") == "keboola_project_mismatch":
        try:
            from app.auth.providers import keboola_verify as _kv

            keboola_expected_project = _kv.configured_project_id() or ""
        except Exception:
            pass
```

and pass `keboola_expected_project=keboola_expected_project` into `_build_context`.

In `app/web/templates/login.html`, extend the `_err_messages` dict (after the
`'google_not_configured'` line, keeping the same dict style):

```jinja
                    'keboola_oauth_failed': "Keboola sign-in failed. Please try again.",
                    'keboola_not_configured': "Keboola sign-in is not configured on this server.",
                    'keboola_not_permitted': "Your Keboola token or project role is not permitted to sign in here. Contact your " ~ (instance_brand or "Agnes") ~ " administrator.",
                    'keboola_project_mismatch': "Your Keboola session belongs to a different project than this instance. Retry and pick project " ~ (keboola_expected_project or "configured for this instance") ~ " on the Keboola sign-in screen.",
```

In `app/auth/mcp_oauth.py::_login_url`, after the google branch (Task 3 form),
add the keboola fallback before the final `/login` return:

```python
    from app.auth.providers.keboola import is_available as keboola_available

    if keboola_available() and provider_allowed("keboola"):
        from urllib.parse import quote

        return f"{base}/auth/keboola/login?next={quote(consent_path)}"
```

- [ ] **Step 6: Run tests**

Run: `.venv/bin/pytest tests/test_keboola_oauth_provider.py tests/test_auth_provider_allowlist.py tests/test_auth_providers.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add app/auth/providers/keboola.py app/main.py app/web/router.py app/web/templates/login.html app/auth/mcp_oauth.py tests/test_keboola_oauth_provider.py
git commit -m "feat: Keboola OAuth web-login provider"
git push
```

---

### Task 7: X-StorageApi-Token header auth + credential classification

**Files:**
- Create: `app/auth/keboola_header.py`
- Modify: `app/auth/dependencies.py:251-255` (header branch), `:372-395` (`require_session_token`)
- Modify: `app/main.py:1940` (elevation `bearer_auth`)
- Test: `tests/test_keboola_auth_header.py`

**Interfaces:**
- Consumes: `keboola_verify` module (Task 5), `switch_value("keboola_token_header")` (Task 4), `trusted_client_ip` (`app.auth.client_ip`).
- Produces: in `app.auth.keboola_header`: `enabled() -> bool`, `resolve_header_user(token: str, request) -> tuple[dict | None, str]` (reason `""` on success; else one of the `KeboolaVerifyError` reasons plus `"keboola_user_unknown"`, `"deactivated"`, `"rate_limited"`), `reset_state_for_tests() -> None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_keboola_auth_header.py
"""X-StorageApi-Token header auth: mapping, precedence, classification,
cache, flood guard — and the require_session_token laundering block."""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.auth.providers import keboola_verify as kv


def _identity(email="jane@example.com"):
    return kv.VerifiedKeboolaIdentity(
        token_id="204", project_id="5947", project_name="Acme DWH",
        email=email, name="Jane", role="admin",
    )


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-32chars-minimum!!!!!")
    monkeypatch.setenv("AGNES_KEBOOLA_ALLOW_TOKEN_HEADER", "1")
    monkeypatch.setattr(kv, "stack_url", lambda: "https://connection.example.com")
    monkeypatch.setattr(kv, "configured_project_id", lambda: "5947")
    from app.auth import keboola_header

    keboola_header.reset_state_for_tests()
    from app.main import create_app
    from src.repositories import users_repo

    app = create_app()
    c = TestClient(app)
    uid = str(uuid.uuid4())
    users_repo().create(id=uid, email="jane@example.com", name="Jane")
    return c


class TestHeaderAuth:
    def test_maps_to_existing_user(self, client, monkeypatch):
        monkeypatch.setattr(kv, "verify_storage_token", lambda tok: _identity())
        resp = client.get("/auth/me", headers={"X-StorageApi-Token": "tok-1"})
        assert resp.status_code == 200
        assert resp.json()["email"] == "jane@example.com"

    def test_unknown_user_gets_onboarding_hint(self, client, monkeypatch):
        monkeypatch.setattr(kv, "verify_storage_token", lambda tok: _identity("nobody@example.com"))
        resp = client.get("/auth/me", headers={"X-StorageApi-Token": "tok-2"})
        assert resp.status_code == 401
        assert "sign in" in resp.json()["detail"].lower()

    def test_switch_off_ignores_header(self, client, monkeypatch):
        monkeypatch.setenv("AGNES_KEBOOLA_ALLOW_TOKEN_HEADER", "0")
        monkeypatch.setattr(kv, "verify_storage_token", lambda tok: _identity())
        resp = client.get("/auth/me", headers={"X-StorageApi-Token": "tok-3"})
        assert resp.status_code == 401

    def test_bearer_takes_precedence(self, client, monkeypatch):
        called = []
        monkeypatch.setattr(kv, "verify_storage_token", lambda tok: called.append(tok) or _identity())
        resp = client.get(
            "/auth/me",
            headers={"Authorization": "Bearer not-a-jwt", "X-StorageApi-Token": "tok-4"},
        )
        # The bogus bearer fails auth; the storage header must NOT rescue it.
        assert resp.status_code == 401
        assert called == []

    def test_verify_cache_hits_within_ttl(self, client, monkeypatch):
        calls = []

        def counting(tok):
            calls.append(tok)
            return _identity()

        monkeypatch.setattr(kv, "verify_storage_token", counting)
        for _ in range(3):
            assert client.get("/auth/me", headers={"X-StorageApi-Token": "tok-5"}).status_code == 200
        assert len(calls) == 1

    def test_cannot_mint_pat(self, client, monkeypatch):
        # The laundering block: a Storage token must never create a persistent PAT.
        monkeypatch.setattr(kv, "verify_storage_token", lambda tok: _identity())
        resp = client.post(
            "/auth/tokens",
            json={"name": "laundered"},
            headers={"X-StorageApi-Token": "tok-6"},
        )
        assert resp.status_code == 403

    def test_flood_guard_trips_on_distinct_invalid_tokens(self, client, monkeypatch):
        def failing(tok):
            raise kv.KeboolaVerifyError("invalid_token", "no")

        monkeypatch.setattr(kv, "verify_storage_token", failing)
        last = None
        for i in range(30):
            last = client.get("/auth/me", headers={"X-StorageApi-Token": f"junk-{i}"})
        assert last.status_code == 429


class TestResolveUnit:
    def test_credential_surface_is_stack(self, client, monkeypatch):
        monkeypatch.setattr(kv, "verify_storage_token", lambda tok: _identity())
        from app.auth.keboola_header import resolve_header_user

        user, reason = resolve_header_user("tok-7", None)
        assert reason == ""
        assert user["credential_surface"] == "stack"
        assert user["token_type"] == "keboola_token"
```

Note: `/auth/me` is assumed to be the standard whoami route on the auth
router — confirm with `grep -n '"/me"\|def me' app/auth/router.py`; if the
route is named differently (e.g. `/auth/whoami`), use that path in every test
above. If none exists, use any cheap authenticated GET (e.g. `/api/tables`)
and assert only on status codes.

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_keboola_auth_header.py -v`
Expected: FAIL (`No module named 'app.auth.keboola_header'`)

- [ ] **Step 3: Implement the header module**

```python
# app/auth/keboola_header.py
"""X-StorageApi-Token header authentication (spec piece 2).

A non-interactive, PAT-like credential: verified per request against the
Keboola stack (60 s positive cache), mapped to an EXISTING user only, pinned
to credential_surface='stack'. Never provisions accounts, never mints
sessions. The in-module flood guard exists because the slowapi route
decorator cannot wrap a dependency — distinct-invalid-token floods must
neither amplify traffic against the customer's stack nor exhaust the
threadpool.
"""

import hashlib
import logging
import threading
import time
from typing import Optional, Tuple

from app.auth.client_ip import trusted_client_ip
from app.auth.providers import keboola_verify as kv

logger = logging.getLogger(__name__)

VERIFY_CACHE_TTL_SECONDS = 60.0
_CACHE_MAX_ENTRIES = 1024

_MISS_WINDOW_SECONDS = 60.0
_MAX_MISSES_PER_IP = 10       # cache-miss verify calls per IP per window
_MAX_MISSES_GLOBAL = 30       # ... and per process per window
_FAILURES_BEFORE_BACKOFF = 5  # consecutive failures from one IP → backoff
_FAILURE_BACKOFF_SECONDS = 60.0

_GLOBAL_KEY = "__global__"

_lock = threading.Lock()
_cache: dict[str, tuple[float, "kv.VerifiedKeboolaIdentity"]] = {}
_miss_windows: dict[str, tuple[float, int]] = {}
_failure_state: dict[str, tuple[float, int]] = {}


def enabled() -> bool:
    from app.switches import switch_value

    if not switch_value("keboola_token_header"):
        return False
    return bool(kv.stack_url() and kv.configured_project_id())


def reset_state_for_tests() -> None:
    with _lock:
        _cache.clear()
        _miss_windows.clear()
        _failure_state.clear()


def _bump_window(key: str, now: float) -> int:
    start, count = _miss_windows.get(key, (now, 0))
    if now - start >= _MISS_WINDOW_SECONDS:
        start, count = now, 0
    _miss_windows[key] = (start, count + 1)
    return count + 1


def _admit_miss(ip: str, now: float) -> Optional[str]:
    """None to admit the upstream verify; 'rate_limited' to refuse."""
    backoff_until, failures = _failure_state.get(ip, (0.0, 0))
    if failures >= _FAILURES_BEFORE_BACKOFF and now < backoff_until:
        return "rate_limited"
    if _bump_window(ip, now) > _MAX_MISSES_PER_IP:
        return "rate_limited"
    if _bump_window(_GLOBAL_KEY, now) > _MAX_MISSES_GLOBAL:
        return "rate_limited"
    return None


def _record_failure(ip: str, now: float) -> None:
    _, failures = _failure_state.get(ip, (0.0, 0))
    failures += 1
    _failure_state[ip] = (now + _FAILURE_BACKOFF_SECONDS, failures)


def _prune_cache(now: float) -> None:
    if len(_cache) <= _CACHE_MAX_ENTRIES:
        return
    for key in [k for k, (ts, _) in _cache.items() if now - ts >= VERIFY_CACHE_TTL_SECONDS]:
        _cache.pop(key, None)


def resolve_header_user(token: str, request) -> Tuple[Optional[dict], str]:
    """(user, "") on success; (None, reason) otherwise. Never raises.

    Only the upstream verify result is cached — the users_repo lookup,
    active check, and downstream RBAC run per request, so an Agnes-side
    deactivation takes effect immediately.
    """
    key = hashlib.sha256(token.encode()).hexdigest()
    now = time.monotonic()
    identity = None
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < VERIFY_CACHE_TTL_SECONDS:
            identity = hit[1]

    if identity is None:
        ip = trusted_client_ip(request) or "unknown"
        with _lock:
            refusal = _admit_miss(ip, now)
        if refusal:
            logger.warning("keboola header verify rate-limited for %s (token sha256=%s…)", ip, key[:12])
            return None, refusal
        try:
            identity = kv.verify_storage_token(token)
        except kv.KeboolaVerifyError as exc:
            with _lock:
                _record_failure(ip, now)
            logger.info("keboola header token rejected: %s (sha256=%s…)", exc.reason, key[:12])
            return None, exc.reason
        with _lock:
            _failure_state.pop(ip, None)
            _cache[key] = (now, identity)
            _prune_cache(now)

    from src.repositories import users_repo

    user = users_repo().get_by_email(identity.email)
    if not user:
        return None, "keboola_user_unknown"
    if not bool(user.get("active", True)):
        return None, "deactivated"
    # PAT-equivalent narrowing: an admin authenticated by a data-plane
    # credential gets the 'stack' read surface, never the implicit 'all'.
    user["credential_surface"] = "stack"
    user["token_type"] = "keboola_token"
    return user, ""
```

- [ ] **Step 4: Wire into `get_current_user`**

In `app/auth/dependencies.py`, add to `_AUTH_DETAIL_BY_REASON`-style handling a
new module-level map (below `_AUTH_DETAIL_BY_REASON`, line ~46):

```python
# X-StorageApi-Token header rejections → 401 detail. Reasons come from
# app.auth.keboola_header.resolve_header_user.
_KEBOOLA_HEADER_DETAIL = {
    "keboola_user_unknown": "No account exists for this Keboola identity — sign in via the web login first",
    "not_master_token": "Only a master (admin) Storage API token can authenticate",
    "no_admin_identity": "The verified token carries no admin identity",
    "project_mismatch": "The token belongs to a different Keboola project than this instance",
    "role_forbidden": "This Keboola project role is not permitted on this instance",
    "deactivated": "Account deactivated",
    "invalid_token": "Invalid or expired token",
    "verify_failed": "Could not verify the token against the Keboola stack",
    "not_configured": "Keboola token authentication is not configured",
}
```

Replace the `if not token: raise HTTPException(...)` block (lines 251-255) with:

```python
    if not token:
        # X-StorageApi-Token is consulted ONLY when no bearer credential and
        # no session cookie are present — a Storage token never shadows an
        # established Agnes credential (spec precedence rule).
        sapi_token = request.headers.get("X-StorageApi-Token") if request is not None else None
        if sapi_token:
            from app.auth.keboola_header import enabled as keboola_header_enabled
            from app.auth.keboola_header import resolve_header_user

            if keboola_header_enabled():
                user, kb_reason = resolve_header_user(sapi_token, request)
                if user:
                    _attach_admin_flag(user, conn)
                    return _stash_user(request, user)
                if kb_reason == "rate_limited":
                    raise HTTPException(
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                        detail="Too many token verification attempts — retry later",
                    )
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=_KEBOOLA_HEADER_DETAIL.get(kb_reason, "Invalid or expired token"),
                )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header",
        )
```

- [ ] **Step 5: Close the laundering hole in `require_session_token`**

In `require_session_token` (line ~372), after the `token` derivation and
before `if token:`, add:

```python
    if not token and request.headers.get("x-storageapi-token"):
        # A request authenticated by the X-StorageApi-Token header is a
        # non-interactive service credential (get_current_user resolved it) —
        # it must never mint PATs, connect MCP, or manage agents, exactly
        # like a PAT. Without this check the header path would be classified
        # as an interactive session because only Authorization/cookie are
        # inspected here.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This endpoint requires an interactive session, not a Storage API token",
        )
```

- [ ] **Step 6: Elevation classification**

In `app/main.py` line ~1940, change the `bearer_auth=` argument to:

```python
                bearer_auth=(
                    request.headers.get("authorization", "").lower().startswith("bearer ")
                    or bool(request.headers.get("x-storageapi-token"))
                ),
```

- [ ] **Step 7: Run tests**

Run: `.venv/bin/pytest tests/test_keboola_auth_header.py tests/test_auth_providers.py tests/auth -v` (drop `tests/auth` if no such dir — check `ls tests/auth` first)
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add app/auth/keboola_header.py app/auth/dependencies.py app/main.py tests/test_keboola_auth_header.py
git commit -m "feat: X-StorageApi-Token header auth with PAT-equivalent classification"
git push
```

---

### Task 8: Wrap-up — snapshot, CHANGELOG, docs, verification loop

**Files:**
- Modify: `CHANGELOG.md` (`## [Unreleased]`), `CLAUDE.md` (Authentication bullet list), OpenAPI snapshot (via make target)
- Test: full local guard loop + CI

- [ ] **Step 1: OpenAPI snapshot**

Run: `make update-openapi-snapshot`
(The new `/auth/keboola/*` routes change the schema; `tests/test_openapi_snapshot.py::test_snapshot_is_fresh` is full-schema equality.)

- [ ] **Step 2: CHANGELOG**

Under `## [Unreleased]` → `### Added` (create the heading if absent):

```markdown
- Keboola login: "Sign in with Keboola" OAuth provider (auto-provisions on first login; binds the instance to one Keboola project via `auth.keboola.project_id`, optional `allowed_roles` narrowing) and opt-in `X-StorageApi-Token` API authentication for existing users (`auth.keboola.allow_token_header`, default off — see docs/feature-flags.md).
- Per-instance auth provider allowlist: `auth.providers` in instance.yaml (env `AGNES_AUTH_PROVIDERS`) narrows which login methods an instance offers; unset keeps today's behavior, excluded providers' endpoints return 404.
```

Under `### Removed`:

```markdown
- Dead `auth.disabled_providers` example config (never consumed) — superseded by `auth.providers`.
```

- [ ] **Step 3: CLAUDE.md**

In the `### Authentication` bullet list (Extensibility section), add:

```markdown
- **Keboola**: OAuth via the Keboola stack (project-bound; optional `X-StorageApi-Token` header auth for existing users, switch-gated)
```

and mention the allowlist in the same block: `Per-instance offering is narrowed by `auth.providers` (see `config/instance.yaml.example`).`

- [ ] **Step 4: Verification loop (verify-agnes-change order)**

```bash
.venv/bin/python scripts/verify_syncmap.py
.venv/bin/pytest tests/test_keboola_identity.py tests/test_auth_provisioning.py tests/test_auth_provider_allowlist.py tests/test_keboola_verify.py tests/test_keboola_oauth_provider.py tests/test_keboola_auth_header.py tests/test_auth_providers.py tests/test_switches.py tests/test_admin_configure_api.py tests/test_openapi_snapshot.py tests/test_route_auth_guard.py tests/test_backend_split_guard.py -q
```

Expected: all PASS. Fix anything that fails and re-run before proceeding.

- [ ] **Step 5: Commit, push, hand to CI**

```bash
git add CHANGELOG.md CLAUDE.md tests/snapshots  # snapshot path per make target output; verify with git status
git commit -m "docs: changelog + auth docs for Keboola login and provider allowlist"
git push
gh pr checks --watch
```

Expected: CI green (this is the full-suite gate). Then run `/agnes-review` on
the diff and fix findings before marking the PR ready.
