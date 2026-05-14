# `/me/profile` Consolidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collapse the four scattered "my account" surfaces (`/profile`, `/me/debug`, `/tokens`) into a single page at `/me/profile`, deleting the old routes/templates outright (no redirects) and updating every reference.

**Architecture:** `/profile` is renamed to `/me/profile` (sibling of `/me/activity`). Its template grows two new Jinja partials — `_profile_tokens.html` (full PAT management, ported from `my_tokens.html`) and `_profile_troubleshooting.html` (session JWT decode + Google-sync snapshot + refetch button, ported from `me_debug.html`). The `me_debug.py` module is slimmed to data-assembly helpers; its `refetch-groups` POST moves into `app/web/router.py`. `/me/debug` and `/tokens` routes and templates are deleted. `/admin/tokens`, `/auth/tokens` (PAT API), and `/api/me/profile` (CLI API) are untouched.

**Tech Stack:** FastAPI, Jinja2 templates (inline `<style>` per template — established pattern), DuckDB, pytest.

**Working context:** Branch `zs/me-activity` (PR #304), fresh clone at `/tmp/agnes-me-activity`. venv at `.venv/`. Run tests with `.venv/bin/pytest <path> -p no:xdist -q`.

---

## File Structure

**Created:**
- `app/web/templates/_profile_tokens.html` — PAT management section partial (list + create/revoke modals + reveal banner + filters/sort + JS). Self-contained `<style>` + `<script>`, ported from `my_tokens.html`.
- `app/web/templates/_profile_troubleshooting.html` — session-diagnostics section partial (user record, decoded JWT claims + fingerprint, Google-sync snapshot, refetch-groups button). Self-contained `<style>` + `<script>`, ported from `me_debug.html`.

**Modified:**
- `app/web/router.py` — rename `/profile` route → `/me/profile`; delete `/tokens` (`my_tokens_page`) route; extend `profile_page` to assemble session-diagnostics context; add `refetch-groups` POST.
- `app/api/me_debug.py` — delete the GET page route, the `router`, and `templates`; keep data-assembly helpers; module becomes import-only.
- `app/main.py` — drop the `me_debug_router` import + `include_router` call.
- `app/web/templates/profile.html` — `{% include %}` the two new partials; drop the "/tokens" link row; widen container.
- `app/web/templates/_app_header.html` — `/profile` → `/me/profile` (href + active check); remove the "Auth debug" menu item.
- `app/web/templates/install.html` — `href="/tokens"` → `href="/me/profile#tokens"` + button label.
- `app/web/templates/admin_tokens.html` — comment text fix.
- `app/api/cli_artifacts.py`, `cli/commands/auth.py`, `cli/skills/security.md` — `/tokens` → `/me/profile#tokens` in user-facing strings.
- `docs/local-development.md`, `docs/HEADLESS_USAGE.md` — `/profile`→`/me/profile`, `/tokens`→`/me/profile`.
- `CHANGELOG.md` — `[Unreleased]` entry.
- Tests: `tests/test_web_ui.py`, `tests/test_me_debug.py`, `tests/test_admin_tokens_ui.py`, `tests/test_groups_mapped_email.py`, `tests/test_auth_providers.py`, `tests/test_pat.py`.

**Deleted:**
- `app/web/templates/my_tokens.html`
- `app/web/templates/me_debug.html`

---

## Task 1: Rename `/profile` → `/me/profile`

Pure route move. No content change. After this task `/me/profile` serves exactly what `/profile` did; `/profile` 404s; every internal reference points to `/me/profile`.

**Files:**
- Modify: `app/web/router.py` (the `@router.get("/profile")` decorator, ~line 2117)
- Modify: `app/web/templates/_app_header.html` (line 112)
- Modify: `tests/test_web_ui.py`, `tests/test_groups_mapped_email.py`, `tests/test_auth_providers.py`
- Modify (comments only): `app/web/templates/profile.html` line 6, `app/api/access.py` line 1009, `tests/test_pat.py`, `tests/test_admin_tokens_ui.py`

- [ ] **Step 1: Update existing tests to the new path (these are the failing tests)**

In `tests/test_web_ui.py`, replace every `"/profile"` request path and `'href="/profile"'` assertion with `/me/profile`. Affected lines: 107, 128, 159 (docstring), 167, 174, 175, 181 (docstring), 188, 200, 210 (docstring), 214.

In `tests/test_groups_mapped_email.py`, replace `"/profile"` with `"/me/profile"` at lines 473, 544 (and the docstrings at 441, 492, 535 — text only).

In `tests/test_auth_providers.py`, replace `"/profile"` with `"/me/profile"` at lines 363, 379 (these tests are `@pytest.mark.skip`'d but keep the path strings correct).

In `tests/test_pat.py` (line 288-290) and `tests/test_admin_tokens_ui.py` (lines 8, 273-275): comment text only — update `/profile` mentions to `/me/profile`.

- [ ] **Step 2: Run the profile tests, verify they fail**

Run: `.venv/bin/pytest tests/test_web_ui.py tests/test_groups_mapped_email.py -p no:xdist -q`
Expected: FAIL — `/me/profile` returns 404 because the route is still `/profile`.

- [ ] **Step 3: Rename the route in `app/web/router.py`**

At `app/web/router.py:2117`, change:
```python
@router.get("/profile", response_class=HTMLResponse)
async def profile_page(
```
to:
```python
@router.get("/me/profile", response_class=HTMLResponse)
async def profile_page(
```
Leave the handler body unchanged.

- [ ] **Step 4: Update the nav link in `_app_header.html`**

At `app/web/templates/_app_header.html:112`, change:
```html
<a class="app-user-menu-item {% if _path == '/profile' %}is-active{% endif %}" role="menuitem" href="/profile">Profile</a>
```
to:
```html
<a class="app-user-menu-item {% if _path == '/me/profile' %}is-active{% endif %}" role="menuitem" href="/me/profile">Profile</a>
```

- [ ] **Step 5: Update stale comment references**

`app/web/templates/profile.html:6` — change `/* /profile — read-only account view ...` to `/* /me/profile — ...`.
`app/api/access.py:1009` — change `the /profile page's` to `the /me/profile page's`.

- [ ] **Step 6: Run the affected tests, verify they pass**

Run: `.venv/bin/pytest tests/test_web_ui.py tests/test_groups_mapped_email.py tests/test_auth_providers.py tests/test_pat.py -p no:xdist -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app/web/router.py app/web/templates/_app_header.html app/web/templates/profile.html app/api/access.py tests/test_web_ui.py tests/test_groups_mapped_email.py tests/test_auth_providers.py tests/test_pat.py tests/test_admin_tokens_ui.py
git commit -m "refactor(web): rename /profile route to /me/profile"
```

---

## Task 2: Migrate the session-diagnostics surface into `/me/profile`

Move everything from `/me/debug` into `/me/profile` as a collapsible "Session & troubleshooting" section, then delete `/me/debug`. The duplicated Group-memberships and Resource-grants sections from `me_debug.html` are **dropped** — `/me/profile` already renders both.

**Files:**
- Create: `app/web/templates/_profile_troubleshooting.html`
- Modify: `app/api/me_debug.py` (delete router + GET route + `templates`; keep helpers)
- Modify: `app/web/router.py` (`profile_page` handler + new `refetch-groups` POST)
- Modify: `app/main.py` (drop `me_debug_router`)
- Modify: `app/web/templates/profile.html` (include the partial)
- Modify: `app/web/templates/_app_header.html` (remove "Auth debug" item)
- Delete: `app/web/templates/me_debug.html`
- Modify: `tests/test_me_debug.py`, `tests/test_web_ui.py`

- [ ] **Step 1: Slim `app/api/me_debug.py` to helpers only**

Delete from `app/api/me_debug.py`:
- the `router = APIRouter(...)` line and the `templates = Jinja2Templates(...)` line (~lines 39, 41)
- the entire `me_debug_page` function and its `@router.get(...)` decorator (~lines 182-228)
- the entire `me_debug_refetch_groups` function and its `@router.post(...)` decorator (~lines 236-301) — its body moves to `router.py` in Step 4
- the now-unused imports: `APIRouter`, `HTMLResponse`, `Jinja2Templates`, `Request` (keep `Depends`, `HTTPException`)

Keep: the module docstring (update its first line to "Session-diagnostic data-assembly helpers for the /me/profile troubleshooting section."), `is_debug_auth_enabled`, `require_debug_auth_enabled`, `_token_fingerprint`, `_read_session_token` (it still takes a `Request` — keep the `Request` import for type hints; if only used in a type hint, `from fastapi import Request` stays), `_decoded_claims`, `_last_sync_summary`.

Delete the now-unused helpers `_user_memberships` and `_accessible_grants` — the merged page does not re-render memberships/grants in the troubleshooting section.

- [ ] **Step 2: Drop the `me_debug_router` from `app/main.py`**

Delete line 98 (`from app.api.me_debug import router as me_debug_router`) and line 614 (`app.include_router(me_debug_router)`).

- [ ] **Step 3: Delete the old template**

```bash
git rm app/web/templates/me_debug.html
```

- [ ] **Step 4: Add the `refetch-groups` POST and extend `profile_page` in `app/web/router.py`**

At the top of `app/web/router.py`, add to the imports:
```python
from app.api.me_debug import (
    require_debug_auth_enabled,
    _read_session_token,
    _decoded_claims,
    _token_fingerprint,
    _last_sync_summary,
)
```

In `profile_page` (now `@router.get("/me/profile")`), after the existing `memberships` assembly and before the `ctx = _build_context(...)` call, add:
```python
    # Session-diagnostics context (formerly the /me/debug page). The
    # troubleshooting section renders the caller's OWN decoded JWT +
    # Google-sync snapshot — their own data, no debug gate on the read.
    _SENSITIVE_USER_COLUMNS = ("password_hash", "setup_token", "reset_token")
    user_record_safe = {
        k: v for k, v in user.items() if k not in _SENSITIVE_USER_COLUMNS
    }
    raw_token = _read_session_token(request)
```
and extend the `_build_context(...)` call to also pass:
```python
        user_record=user_record_safe,
        claims=_decoded_claims(raw_token),
        token_fingerprint=_token_fingerprint(raw_token),
        sync_summary=_last_sync_summary(user["id"], conn),
        google_group_prefix=os.environ.get("AGNES_GOOGLE_GROUP_PREFIX", "").strip(),
```
(`os` is already imported in `router.py` — it is used by `profile_page` already.)

Immediately after the `profile_page` function, add the relocated POST:
```python
@router.post("/me/profile/refetch-groups", name="me_profile_refetch_groups")
async def me_profile_refetch_groups(
    _: None = Depends(require_debug_auth_enabled),
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Re-issue ``fetch_user_groups`` for the current user and return a
    dry-run diff against the cached ``user_group_members`` snapshot,
    writing nothing. Gated behind AGNES_DEBUG_AUTH — a dry-run admin
    debug action, not user-facing content."""
    from app.auth.group_sync import fetch_user_groups

    fetched = fetch_user_groups(user["email"])
    soft_failed = fetched is None
    fetched_list = list(fetched) if fetched else []

    # display-only env reads use module-level `os` (already imported in router.py)
    prefix = os.environ.get("AGNES_GOOGLE_GROUP_PREFIX", "").strip().lower()
    if prefix:
        relevant = [g.lower() for g in fetched_list if g.lower().startswith(prefix)]
    else:
        relevant = [g.lower() for g in fetched_list]

    has_ext = conn.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'user_groups' AND column_name = 'external_id'"
    ).fetchone()
    select_ext = "g.external_id" if has_ext else "NULL"
    current_rows = conn.execute(
        f"""SELECT g.name, {select_ext} AS external_id
              FROM user_group_members m
              JOIN user_groups g ON g.id = m.group_id
             WHERE m.user_id = ? AND m.source = 'google_sync'
             ORDER BY g.name""",
        [user["id"]],
    ).fetchall()
    current_external_ids = {r[1].lower() for r in current_rows if r[1]}
    current_names = [r[0] for r in current_rows]

    fetched_set = set(relevant)
    would_add = sorted(fetched_set - current_external_ids)
    would_remove = sorted(current_external_ids - fetched_set) if has_ext else []

    return {
        "soft_failed": soft_failed,
        "prefix": prefix or None,
        "fetched": fetched_list,
        "fetched_relevant": relevant,
        "current_names": current_names,
        "current_external_ids": sorted(current_external_ids),
        "would_add": would_add,
        "would_remove": would_remove,
        "applied": False,
    }
```

- [ ] **Step 5: Create `app/web/templates/_profile_troubleshooting.html`**

Port the **User record**, **Session JWT (decoded)**, and **Last Google sync snapshot** sections from the deleted `me_debug.html` (recover it with `git show HEAD~1:app/web/templates/me_debug.html` if needed — it was deleted in Step 3, so do this step against a copy taken before deletion, or `git show` it).

Transformations:
- No `{% extends %}` / `{% block %}` — this is an `{% include %}`-d partial: emit only a `<style>` block + the section markup + a `<script>` block.
- Wrap the whole thing in `<section class="section-card">` blocks matching `profile.html`'s vocabulary (`<h3>` uppercase headers), NOT the old `.md-section` classes. Rename `.md-section*` CSS to `section-card`-compatible rules or drop them in favour of `profile.html`'s existing `.section-card`/`.empty-state` classes.
- **Drop** the "Group memberships" and "Resource grants (effective)" sections — `/me/profile` already renders both above this partial.
- The refetch-groups button + its JS: keep, but the `fetch("/me/debug/refetch-groups", ...)` call becomes `fetch("/me/profile/refetch-groups", ...)`. Wrap the button in `{% if config.DEBUG_AUTH_ENABLED %}...{% endif %}` so it only renders on dev instances (the POST keeps its `require_debug_auth_enabled` gate). The JWT-decode + sync-snapshot read-only content renders unconditionally.
- Template variables consumed: `user_record`, `claims`, `token_fingerprint`, `sync_summary`, `google_group_prefix` — all now supplied by `profile_page` (Step 4).

- [ ] **Step 6: Include the partial in `profile.html`**

In `app/web/templates/profile.html`, after the `Effective access` section (after line ~297, before the closing `</div>` of `.profile-page`), add:
```html
  <details class="section-card" aria-label="Session and troubleshooting">
    <summary>Session &amp; troubleshooting</summary>
    {% include "_profile_troubleshooting.html" %}
  </details>
```
`<summary>` only permits phrasing content — do NOT nest an `<h3>` inside it. Put the text directly in `<summary>` and add a CSS rule in `profile.html`'s `<style>` styling `details.section-card > summary` to match the `.section-card h3` look (uppercase, `letter-spacing: 0.4px`, `font-size: 13px`, `font-weight: 600`, `color: var(--text-secondary)`, `cursor: pointer`).

- [ ] **Step 7: Remove the "Auth debug" menu item from `_app_header.html`**

Delete the `<a ... href="/me/debug">Auth debug</a>` line (~line 121) and the surrounding `{# Auth debug now hosts ... #}` comment block (~lines 113-120) added by commit `5414cb0e`.

- [ ] **Step 8: Rework `tests/test_me_debug.py`**

The `TestGating` class tested the GET page. The GET page no longer exists. Replace the whole file's intent: keep only the `refetch-groups` POST tests, re-pointed at `/me/profile/refetch-groups`. Update the module docstring. Delete the `test_get_returns_200_regardless_of_flag` and `test_returns_200_for_authed_user_when_flag_on` tests (they hit the deleted GET page). Keep the POST tests at lines 156, 182, 228 — change `c.post("/me/debug/refetch-groups", ...)` to `c.post("/me/profile/refetch-groups", ...)`. Rename the file conceptually to cover "refetch-groups POST" only.

In `tests/test_web_ui.py`, delete the `assert 'href="/me/debug"' in body` assertions at lines 109, 132, 150 and add `assert 'href="/me/debug"' not in body` to the nav tests; the menu is now Profile + My activity only.

- [ ] **Step 9: Run the affected tests, verify they pass**

Run: `.venv/bin/pytest tests/test_me_debug.py tests/test_web_ui.py -p no:xdist -q`
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add app/api/me_debug.py app/main.py app/web/router.py app/web/templates/_profile_troubleshooting.html app/web/templates/profile.html app/web/templates/_app_header.html tests/test_me_debug.py tests/test_web_ui.py
git rm app/web/templates/me_debug.html
git commit -m "refactor(web): fold /me/debug into /me/profile troubleshooting section"
```

---

## Task 3: Migrate the PAT surface into `/me/profile`

Inline the full `/tokens` experience as a `Personal Authentication Tokens` section on `/me/profile`, then delete `/tokens`.

**Files:**
- Create: `app/web/templates/_profile_tokens.html`
- Modify: `app/web/router.py` (delete `my_tokens_page` route)
- Modify: `app/web/templates/profile.html` (include partial, drop link row, widen container)
- Delete: `app/web/templates/my_tokens.html`
- Modify: `tests/test_admin_tokens_ui.py`, `tests/test_web_ui.py`

- [ ] **Step 1: Create `app/web/templates/_profile_tokens.html`**

Port the entire body of `my_tokens.html` (hero action button, reveal banner, toolbar with status/sort chips, token card list, revoke modal, create modal, toast stack, and the full `<script>`).

Transformations:
- No `{% extends %}` / `{% block %}` — emit `<style>` + markup + `<script>` only.
- Drop the standalone `.tokens-hero` gradient hero block (lines ~665-681 of `my_tokens.html`). Replace it with a `<section class="section-card">` whose `<h3>` is `Personal Authentication Tokens` and a one-line subtitle: `Bearer tokens for the API + CLI (the agnes commands). NOT LLM tokens — create / revoke here; the LLM token counters live on /me/activity.` Move the "New token" button (`#new-token-btn`) to sit beside that `<h3>` (right-aligned).
- Keep the wrapper element's `data-is-admin="false" data-view="my"` attributes — JS and tests read them. Change the outer `<div class="tokens-page" ...>` to `<div class="tokens-section" data-is-admin="false" data-view="my">` and rename the `.tokens-page` CSS rule to `.tokens-section` (drop the `body > .container` override line — the container width is set by `profile.html`, see Step 3).
- Keep all element IDs unchanged (`#new-token-btn`, `#reveal-banner`, `#flt-status`, `#flt-status-group`, `#sort-group`, `#flt-last-used`, `#flt-user`, `#tokens-list`, `#tokens-loading`, `#tokens-empty`, `#confirm-modal`, `#create-modal`, `#toast-stack`, etc.) — `tests/test_admin_tokens_ui.py` asserts on them.
- Keep the `<script>` API constants unchanged: `API_LIST = "/auth/tokens"`, `API_REVOKE`, `API_CREATE` — the PAT API does not move.
- The modals (`#confirm-modal`, `#create-modal`) and `#toast-stack` are `position: fixed` — they render fine from inside an included partial; leave them at the end of the partial.

- [ ] **Step 2: Delete the old template and route**

```bash
git rm app/web/templates/my_tokens.html
```
In `app/web/router.py`, delete the `@router.get("/tokens", ...)` decorator and the entire `my_tokens_page` function (~lines 2088-2099).

- [ ] **Step 3: Wire the partial into `profile.html` and widen the container**

In `app/web/templates/profile.html`:
- Change both `max-width: 960px` occurrences (the `body > .container` rule at line 8 and `.profile-page` at line 11) to `max-width: 1100px` — the token card list needs the room.
- Delete the `.tokens-link-row` CSS block (lines ~144-156) and the `<div class="tokens-link-row">Manage personal access tokens at <a href="/tokens">/tokens</a>.</div>` markup inside the Account section (lines ~254-256) — tokens are now on this page.
- Insert `{% include "_profile_tokens.html" %}` as a new `<section>`-level block between the `Effective access` section and the `Session & troubleshooting` `<details>` added in Task 2.

- [ ] **Step 4: Update `tests/test_admin_tokens_ui.py`**

The non-admin / admin-own `/tokens` tests (`test_non_admin_sees_my_tokens_page` ~line 76, the admin-own test ~line 119, ~line 148) request `"/tokens"`. Change those request paths to `"/me/profile"`. The assertions on `#new-token-btn`, `#create-modal`, `data-is-admin="false"`, "My tokens" body copy still hold because the partial preserves those IDs/attributes — but update any assertion on the literal page `<title>` or hero `<h2>My tokens</h2>` to the new `<h3>Personal Authentication Tokens</h3>`. The `/admin/tokens` tests in the same file are unchanged. Update the file header comment (lines 4, 8).

In `tests/test_web_ui.py`, change the `assert 'href="/tokens"' in body` assertion (line 171) to `assert 'href="/tokens"' not in body` (the nav no longer links `/tokens`).

- [ ] **Step 5: Run the affected tests, verify they pass**

Run: `.venv/bin/pytest tests/test_admin_tokens_ui.py tests/test_web_ui.py tests/test_pat.py -p no:xdist -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/web/templates/_profile_tokens.html app/web/router.py app/web/templates/profile.html tests/test_admin_tokens_ui.py tests/test_web_ui.py
git rm app/web/templates/my_tokens.html
git commit -m "refactor(web): fold /tokens PAT management into /me/profile"
```

---

## Task 4: Update remaining external references

Mechanical string updates outside the route/template/test core. No behaviour change.

**Files:**
- Modify: `app/web/templates/install.html`, `app/web/templates/admin_tokens.html`, `app/api/cli_artifacts.py`, `cli/commands/auth.py`, `cli/skills/security.md`, `docs/local-development.md`, `docs/HEADLESS_USAGE.md`

- [ ] **Step 1: Update `install.html`**

At `app/web/templates/install.html:806-807`, change:
```html
<a href="/tokens" class="btn-cta">
    Open /tokens
```
to:
```html
<a href="/me/profile#tokens" class="btn-cta">
    Open /me/profile#tokens
```
(href and label must agree — the label mirrors the literal path, as the old `Open /tokens` did.) Update the comment at line 473 (`Open /tokens etc.`) and line 1034 (`from the /tokens UI`) to say `/me/profile`.

- [ ] **Step 2: Update CLI-facing strings**

- `app/api/cli_artifacts.py:151` — change `at $SERVER/tokens` to `at $SERVER/me/profile`.
- `cli/commands/auth.py:57` — change `open /tokens, and create` to `open /me/profile, and create`.
- `cli/skills/security.md:60` — change `via the UI (`/tokens` → New token)` to `via the UI (`/me/profile` → New token)`.

- [ ] **Step 3: Update comment in `admin_tokens.html`**

`app/web/templates/admin_tokens.html:10` — change `admins use /tokens for their own` to `admins use /me/profile for their own`.

- [ ] **Step 4: Update docs**

- `docs/local-development.md` lines 11, 25, 54, 84 — change `/profile` to `/me/profile`.
- `docs/HEADLESS_USAGE.md` lines 9, 47 — change `/tokens` to `/me/profile`.

(Historical `docs/superpowers/plans/*.md` and `CHANGELOG.md` history entries are NOT touched — they are point-in-time records.)

- [ ] **Step 5: Verify nothing else references the dead paths**

Run:
```bash
grep -rnE '/me/debug|"/tokens"|href="/profile"' --include="*.py" --include="*.html" --include="*.js" app/ cli/ docs/local-development.md docs/HEADLESS_USAGE.md
```
Expected: no matches except `/admin/tokens`, `/auth/tokens`, `/api/me/profile`, and `/me/profile/refetch-groups`. If anything else appears, fix it.

- [ ] **Step 6: Commit**

```bash
git add app/web/templates/install.html app/web/templates/admin_tokens.html app/api/cli_artifacts.py cli/commands/auth.py cli/skills/security.md docs/local-development.md docs/HEADLESS_USAGE.md
git commit -m "refactor: repoint /profile, /me/debug, /tokens references to /me/profile"
```

---

## Task 5: CHANGELOG + full verification

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Update the `[Unreleased]` CHANGELOG entry**

In `CHANGELOG.md`, the existing `### Changed` bullet under `[Unreleased]` still claims the user menu reads "Profile → My activity → My tokens" — that is now wrong. Replace that bullet, and add the consolidation. Under `[Unreleased]`:

In `### Changed`, replace the "Top-nav Stats / user menu" bullet with:
```markdown
- `/profile` is renamed to `/me/profile` and absorbs the former
  `/me/debug` (session diagnostics) and `/tokens` (Personal
  Authentication Token management) pages: one account page with
  Account, Group memberships, Effective access, Personal
  Authentication Tokens, and a collapsible Session & troubleshooting
  section. The user menu is now Profile → My activity. Top-nav "Stats"
  was already removed; "My tokens" and "Auth debug" menu entries are
  retired. `/admin/tokens`, the `/auth/tokens` API, and `/api/me/profile`
  are unchanged.
```

In `### Removed`, add:
```markdown
- `/profile`, `/me/debug`, and `/tokens` routes and their templates
  (`me_debug.html`, `my_tokens.html`) — no redirects; all internal
  links were repointed to `/me/profile`. The `/me/debug/refetch-groups`
  POST moved to `/me/profile/refetch-groups` (still gated behind
  `AGNES_DEBUG_AUTH`).
```

- [ ] **Step 2: Run the full test suite**

Run: `.venv/bin/pytest tests/ --tb=short -n auto -q`
Expected: PASS, except the 11 pre-existing `test_keboola_*` failures (`ModuleNotFoundError: No module named 'kbcstorage'` — a missing optional dep, unrelated to this change, reproduces on clean `main`).

- [ ] **Step 3: Manual smoke check (optional but recommended)**

Run `uvicorn app.main:app` locally, sign in, and confirm `/me/profile` renders all five sections, the create/revoke token flow works, the troubleshooting `<details>` expands, and `/profile` / `/me/debug` / `/tokens` all 404.

- [ ] **Step 4: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): /me/profile consolidation"
```

---

## Self-Review

**Spec coverage:**
- `/profile` → `/me/profile` rename — Task 1. ✓
- `/me/debug` content folded in, route deleted, no redirect — Task 2. ✓
- `/tokens` content folded in, route deleted, no redirect — Task 3. ✓
- All references updated (templates, CLI, docs, tests) — Tasks 1–4, with a grep gate in Task 4 Step 5. ✓
- `refetch-groups` POST relocated, gate preserved — Task 2 Step 4. ✓
- `/admin/tokens`, `/auth/tokens`, `/api/me/profile` untouched — explicitly excluded throughout. ✓
- `/profile/sessions/{filename}` download endpoint — out of scope (belongs to `/me/activity`), not touched by any task. ✓
- Style preserved (bespoke `.section-card` / token-card vocabulary, not canonical primitives) — Task 2 Step 5, Task 3 Step 1. ✓
- CHANGELOG, including the stale "My tokens" bullet fix — Task 5. ✓

**Placeholder scan:** No "TBD"/"handle edge cases"/"similar to". The two template ports (Task 2 Step 5, Task 3 Step 1) give explicit transformation specs against named source files rather than re-inlining ~1600 lines of template code — this is a port, not greenfield, so the source file IS the spec.

**Type/name consistency:** `me_profile_refetch_groups` route name and `/me/profile/refetch-groups` path used consistently in Task 2 (router) and Task 2 Step 8 (test). Partial filenames `_profile_tokens.html` / `_profile_troubleshooting.html` consistent across File Structure, Task 2, Task 3. Element IDs preserved verbatim from `my_tokens.html` so `test_admin_tokens_ui.py` assertions hold.

**Coordination note:** Task 2 deletes `me_debug.html` and Task 2/3 supersede commit `5414cb0e` (Vojta's `feat(me/debug)` PAT-on-debug-page work). Vojta is a co-author on PR #304 — sync with him before executing so the rebase/overwrite is expected.
