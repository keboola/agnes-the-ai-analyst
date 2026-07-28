# Phase 4 — Web badge + cross-surface deep link Implementation Plan
> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Surface Slack-origin sessions in the web `/chat` sidebar with a non-interactive "Slack" pill, and make `/chat?session=<id>` deep-link directly into a session on page load — both client-side-only, degrading gracefully on older servers.

**Architecture:** `GET /api/chat/sessions` already emits `surface` (one of `web` / `slack_dm` / `slack_thread`). The badge is a pure JS render in `_makeSidebarItem(s)` (chat.js) styled by a new `.cloud-chat-surface-badge` CSS class in `app/web/static/css/chat.css` using `var(--ds-*)` tokens only. The deep link is a query param read by the `chat_page()` route handler (router.py), exposed as a DOM hook on `<body>` in `chat.html`, and consumed exactly once on boot by chat.js via `requestAnimationFrame(() => openSession(id))`, guarded by `&& !currentChatId` and nulled after consumption. No new endpoints, no DB changes, no RBAC surface change — RBAC stays enforced by the existing session-scoped endpoints the deep-link's `openSession` calls.

**Tech Stack:** FastAPI + Jinja2 (`app/web/router.py`, `app/web/templates/chat.html`), vanilla ES module JS (`app/web/static/js/chat.js`), CSS design tokens (`app/web/static/css/chat.css`), pytest + Starlette `TestClient`.

---

## File Structure

**Modified**
- `app/web/static/js/chat.js` — (1) `_makeSidebarItem(s)` appends a `.cloud-chat-surface-badge` pill for `slack_dm`/`slack_thread` surfaces; (2) new module-level `_initialSessionId` read once on boot from the body DOM hook and one-shot-consumed via `_maybeOpenInitialSession()`.
- `app/web/static/css/chat.css` — new `.cloud-chat-surface-badge` rule (design tokens only).
- `app/web/router.py` — `chat_page()` reads `request.query_params.get("session")` into `ctx["initial_session_id"]` (no 404 on unknown/forbidden).
- `app/web/templates/chat.html` — `body_attrs` block emits `data-initial-session="{{ initial_session_id or '' }}"` as the DOM hook.
- `CHANGELOG.md` — one bullet under `## [Unreleased]`.

**Created**
- `tests/test_chat_web_deeplink.py` — router-side test: `chat_page` threads the `session` query param into the rendered body hook and never 404s.
- `tests/test_chat_surface_badge.py` — static-source guards over `chat.js` + `chat.css` proving the badge render path and the design-token-only CSS exist (no headless-browser dependency in CI).

No new repo methods, so no `_pg.py` sibling and no `tests/db_pg/` change is in scope for this phase.

---

## Task 1 — Router: thread the `?session=` query param into the page context

**Files:**
- Modify: `app/web/router.py` (function `chat_page` at line 3166)
- Test: `tests/test_chat_web_deeplink.py` (Create)

- [ ] Create `tests/test_chat_web_deeplink.py` with the same minimal-app fixture pattern used by `tests/test_chat_web_route.py` (build a FastAPI app with the web router, set `app.state.chat_config`, override `get_current_user`, patch `can_access`/`has_explicit_grant` True, pin `DATA_DIR` to a tmp dir). Write the first failing test:

```python
"""Deep-link: /chat?session=<id> threads the param into the page DOM hook
without 404-ing on unknown/forbidden ids (RBAC is enforced later by the
session-scoped JS endpoints, not by the page route)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth.dependencies import get_current_user

TEST_USER = {"id": "user1", "email": "alice@test.com", "is_admin": False}


def _make_app(*, chat_enabled: bool = True) -> FastAPI:
    from app.web.router import router as web_router

    app = FastAPI()
    app.include_router(web_router)
    app.state.chat_config = SimpleNamespace(enabled=chat_enabled)
    app.dependency_overrides[get_current_user] = lambda: TEST_USER
    return app


@pytest.fixture(autouse=True)
def _grant_chat_access(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "sysdb"))
    import app.auth.access as _access

    monkeypatch.setattr(_access, "can_access", lambda *a, **k: True)
    monkeypatch.setattr(_access, "has_explicit_grant", lambda *a, **k: True)


@pytest.fixture
def api_client() -> TestClient:
    return TestClient(_make_app(chat_enabled=True))


def test_deep_link_param_renders_in_body_hook(api_client: TestClient):
    r = api_client.get("/chat?session=sess-abc123")
    assert r.status_code == 200
    assert 'data-initial-session="sess-abc123"' in r.text


def test_no_param_renders_empty_hook(api_client: TestClient):
    r = api_client.get("/chat")
    assert r.status_code == 200
    assert 'data-initial-session=""' in r.text


def test_unknown_session_id_does_not_404(api_client: TestClient):
    # The route NEVER validates the id — ownership is enforced by the
    # session-scoped endpoints the JS calls. Page always renders 200.
    r = api_client.get("/chat?session=does-not-exist-or-forbidden")
    assert r.status_code == 200
    assert 'data-initial-session="does-not-exist-or-forbidden"' in r.text
```

- [ ] Run it, expect FAIL: `.venv/bin/pytest tests/test_chat_web_deeplink.py -v` — all three fail because `chat.html` does not yet render `data-initial-session` (`assert 'data-initial-session=...' in r.text` → AssertionError).
- [ ] In `app/web/router.py`, in `chat_page`, add the query-param read right after `ctx = _build_context(...)` (line 3191), before the `return`:

```python
    ctx = _build_context(request, user=user, conn=conn, current_user=user)
    ctx["chat_capabilities"] = _chat_capability_snapshot(conn, user)
    # Deep link: /chat?session=<id>. We DO NOT validate the id here (no
    # 404 on unknown/forbidden) — the page always renders and RBAC is
    # enforced when chat.js calls the session-scoped endpoints
    # (POST /sessions/{id}/ticket, GET /sessions/{id}/messages), which
    # carry the existing ownership guards. A bad id fails those calls and
    # surfaces an error status in the UI; the page itself still renders.
    ctx["initial_session_id"] = request.query_params.get("session")
    return templates.TemplateResponse(request, "chat.html", ctx)
```

- [ ] Add the DOM hook to `app/web/templates/chat.html`. Change the `body_attrs` block (line 17) from:

```html
{% block body_attrs %}class="chat-page-body" data-user-email="{{ current_user.email }}"{% endblock %}
```

to:

```html
{% block body_attrs %}class="chat-page-body" data-user-email="{{ current_user.email }}" data-initial-session="{{ initial_session_id or '' }}"{% endblock %}
```

- [ ] Run it, expect PASS: `.venv/bin/pytest tests/test_chat_web_deeplink.py -v` — all three pass.
- [ ] Run the existing route test to confirm no regression: `.venv/bin/pytest tests/test_chat_web_route.py -v` — green.
- [ ] Commit:

```bash
git add app/web/router.py app/web/templates/chat.html tests/test_chat_web_deeplink.py
git commit -m "chat: thread ?session= deep-link param into /chat body hook"
```

---

## Task 2 — JS: one-shot deep-link auto-open on boot

**Files:**
- Modify: `app/web/static/js/chat.js` (boot IIFE at lines 1666–1671; module-state region near the top, lines 4–6)
- Test: `tests/test_chat_surface_badge.py` (Create — shared file for both JS-side static guards; this task adds the deep-link guards, Task 3 adds the badge guards)

- [ ] Create `tests/test_chat_surface_badge.py` with the deep-link static guards as the first failing tests. These assert the JS source contains the one-shot auto-open machinery (no headless browser in CI; we guard the source contract the way `tests/test_design_system_contract.py` guards CSS/HTML source):

```python
"""Static-source guards for the cross-surface badge + deep-link auto-open
in chat.js / chat.css. No headless browser in CI — we assert the source
contract the way test_design_system_contract.py guards templates/CSS."""
from pathlib import Path

CHAT_JS = Path("app/web/static/js/chat.js")
CHAT_CSS = Path("app/web/static/css/chat.css")


def _js() -> str:
    return CHAT_JS.read_text(encoding="utf-8")


# --- Deep-link one-shot auto-open ---------------------------------------

def test_js_reads_initial_session_from_body_hook():
    js = _js()
    # Reads the DOM hook emitted by chat.html's body_attrs block.
    assert "dataset.initialSession" in js


def test_js_deep_link_is_one_shot_and_guarded():
    js = _js()
    # Guarded so a later sidebar refresh can't re-hijack the view, and
    # consumed exactly once (set to null after use).
    assert "_initialSessionId" in js
    assert "!currentChatId" in js
    assert "requestAnimationFrame" in js


def test_js_deep_link_open_helper_defined():
    js = _js()
    assert "_maybeOpenInitialSession" in js
```

- [ ] Run them, expect FAIL: `.venv/bin/pytest tests/test_chat_surface_badge.py -v` — the three deep-link tests fail (`dataset.initialSession`, `_initialSessionId`, `_maybeOpenInitialSession` not yet in source).
- [ ] In `app/web/static/js/chat.js`, add module-level deep-link state right after `let inFlightToolCalls = new Map();` (line 6):

```javascript
let inFlightToolCalls = new Map();

// --- Cross-surface deep link (/chat?session=<id>) ------------------------
// chat.html's <body data-initial-session="<id>"> hook carries an optional
// session id from the ?session= query param. We open it ONCE on boot,
// after the sidebar cache is populated, and only if the user hasn't
// already navigated into a session (``!currentChatId``). Consumed once
// (set to null) so a later loadSidebar() refresh can't re-hijack the view.
// On an unknown / forbidden id, openSession proceeds (it sets currentChatId
// and clears the message pane) but its session-scoped endpoint calls
// (GET /sessions/{id}/messages, POST /sessions/{id}/ticket) fail their RBAC
// guards and surface a status message via setStatus — no page crash, no
// data leak; the view simply lands on an empty "Untitled chat" with an
// error status. (This is not a clean no-op: a bad deep link leaves the UI
// in an empty/error state, which is acceptable and RBAC-safe.)
let _initialSessionId = (document.body.dataset.initialSession || "").trim() || null;

/** Open the deep-linked session exactly once on boot. No-op if there's no
 *  deep link, if the user already opened a session, or after first use. */
function _maybeOpenInitialSession() {
  if (!_initialSessionId || currentChatId) return;
  const id = _initialSessionId;
  _initialSessionId = null;            // consume once — refreshes can't re-fire
  requestAnimationFrame(() => {
    if (currentChatId) return;          // re-check: a click may have raced in
    openSession(id);
  });
}
```

- [ ] Wire the call into the boot IIFE at the bottom of the file. Change (lines 1666–1671):

```javascript
(async () => {
  renderCapabilities();
  wireSuggestionButtons();
  autosizeComposer();
  await loadSidebar();
})();
```

to:

```javascript
(async () => {
  renderCapabilities();
  wireSuggestionButtons();
  autosizeComposer();
  await loadSidebar();
  // Sidebar cache (_sessionsCache) is now populated so openSession can
  // resolve the title; fire the one-shot deep-link open.
  _maybeOpenInitialSession();
})();
```

- [ ] Run the tests, expect PASS: `.venv/bin/pytest tests/test_chat_surface_badge.py -v -k "deep_link or initial_session"` — the three deep-link tests pass.
- [ ] Commit:

```bash
git add app/web/static/js/chat.js tests/test_chat_surface_badge.py
git commit -m "chat: one-shot deep-link auto-open from ?session= body hook"
```

---

## Task 3 — JS + CSS: "Slack" surface badge pill in the sidebar

**Files:**
- Modify: `app/web/static/js/chat.js` (function `_makeSidebarItem` at line 229)
- Modify: `app/web/static/css/chat.css` (append a new rule block)
- Test: `tests/test_chat_surface_badge.py` (extend with badge guards)

- [ ] Extend `tests/test_chat_surface_badge.py` with the badge guards (append to the file). These assert the JS emits the pill for the two Slack surfaces and the CSS rule exists using design tokens only:

```python
# --- Surface badge (Slack pill) -----------------------------------------

def test_js_badge_class_emitted_in_make_sidebar_item():
    js = _js()
    assert "cloud-chat-surface-badge" in js
    # Both Slack surfaces trigger the pill; web does not.
    assert "slack_dm" in js
    assert "slack_thread" in js


def test_js_badge_text_is_slack_not_icon():
    js = _js()
    # Text label, not a brand asset (design-system contract: no bundled
    # Slack icon). The pill's textContent is the literal "Slack".
    assert '"Slack"' in js or "'Slack'" in js


def test_chat_css_has_surface_badge_rule():
    css = CHAT_CSS.read_text(encoding="utf-8")
    assert ".cloud-chat-surface-badge" in css


def test_chat_css_surface_badge_uses_only_ds_tokens():
    """The badge rule must reference design tokens (var(--ds-*)) and contain
    NO raw #hex literal and NO legacy var(--primary). Mirrors the design-
    system contract that test_design_system_contract.py enforces on
    templates — applied here to the new chat.css rule block."""
    import re

    css = CHAT_CSS.read_text(encoding="utf-8")
    # Isolate the badge rule block: from the selector to its closing brace.
    m = re.search(r"\.cloud-chat-surface-badge\s*\{(.*?)\}", css, re.DOTALL)
    assert m, "could not locate .cloud-chat-surface-badge rule block"
    block = m.group(1)
    assert "var(--ds-" in block, "badge must use --ds-* design tokens"
    assert not re.search(r"#[0-9a-fA-F]{3,6}\b", block), "no raw hex allowed"
    assert not re.search(r"var\(\s*--primary[-)\s,]", block), (
        "use var(--ds-primary…), not legacy var(--primary…)"
    )
```

- [ ] Run them, expect FAIL: `.venv/bin/pytest tests/test_chat_surface_badge.py -v -k "badge or surface"` — the four badge tests fail (`cloud-chat-surface-badge` not in JS/CSS yet; the CSS-block regex finds nothing).
- [ ] In `app/web/static/js/chat.js`, edit `_makeSidebarItem` to append the pill after the label span. The current label append is (lines 245–248):

```javascript
  const label = document.createElement("span");
  label.className = "cloud-chat-list-label";
  label.textContent = s.title || "Untitled chat";
  li.appendChild(label);
```

Change to:

```javascript
  const label = document.createElement("span");
  label.className = "cloud-chat-list-label";
  label.textContent = s.title || "Untitled chat";
  li.appendChild(label);

  // Cross-surface origin pill. Slack-originated sessions (slack_dm /
  // slack_thread) get a small, non-interactive "Slack" text pill so the
  // user can tell at a glance which conversations came in over Slack vs
  // the web composer. Text, not a brand icon — no asset bundled, satisfies
  // the design-system contract. Unknown / undefined surface → no pill
  // (fail-closed: an older server that doesn't emit `surface` shows the
  // plain web style).
  if (s.surface === "slack_dm" || s.surface === "slack_thread") {
    const badge = document.createElement("span");
    badge.className = "cloud-chat-surface-badge";
    badge.textContent = "Slack";
    badge.setAttribute("aria-hidden", "true");  // label already names the row
    li.appendChild(badge);
  }
```

- [ ] In `app/web/static/css/chat.css`, append the badge rule at the end of the file. Use design tokens only — no raw hex, no `var(--primary)`:

```css
/* --- Cross-surface origin pill ------------------------------------------
 * Non-interactive "Slack" pill rendered in the sidebar for slack_dm /
 * slack_thread sessions (see _makeSidebarItem in chat.js). Text label
 * only — no brand icon asset, satisfying the design-system contract.
 * Colour rides the info-accent tokens so it reads as a quiet metadata
 * chip, not an action. Tokens only (var(--ds-*)); no raw hex. */
.cloud-chat-surface-badge {
  flex: 0 0 auto;
  margin-left: var(--space-2);
  padding: 0 var(--space-2);
  border: 1px solid var(--ds-accent-info-line);
  border-radius: var(--radius-pill, 999px);
  background: var(--ds-accent-info-bg);
  color: var(--ds-accent-info-ink);
  font-size: var(--text-xs);
  line-height: 1.6;
  letter-spacing: 0.02em;
  text-transform: uppercase;
  pointer-events: none;
  user-select: none;
}

/* Mini (collapsed) sidebar shows only initials — hide the pill so the
 * 56px rail stays clean. */
.cloud-chat-shell.is-mini .cloud-chat-surface-badge {
  display: none;
}
```

- [ ] Run the badge tests, expect PASS: `.venv/bin/pytest tests/test_chat_surface_badge.py -v` — all seven tests (3 deep-link + 4 badge) pass.
- [ ] Run the design-system contract suite to confirm the new CSS doesn't trip any guard (`_CANONICAL_CSS` now scans `chat.css`): `.venv/bin/pytest tests/test_design_system_contract.py -v` — green.
- [ ] Commit:

```bash
git add app/web/static/js/chat.js app/web/static/css/chat.css tests/test_chat_surface_badge.py
git commit -m "chat: Slack surface pill in /chat sidebar (tokens-only CSS)"
```

---

## Task 4 — Full-suite verification + CHANGELOG

**Files:**
- Modify: `CHANGELOG.md` (under `## [Unreleased]`, line 11)

- [ ] Run the full suite the way CI does: `.venv/bin/pytest tests/ --tb=short -n auto -q`. Expect all green. If a failure is in code this phase touched (`tests/test_chat_web_route.py`, `tests/test_chat_web_deeplink.py`, `tests/test_chat_surface_badge.py`, `tests/test_design_system_contract.py`, `tests/test_chat_api.py`), fix before continuing. If a failure is unrelated to this diff, confirm it reproduces on a clean tree (`git stash` → re-run → `git stash pop`) and note it; do not block on it.
- [ ] Add a bullet under `## [Unreleased]` in `CHANGELOG.md`. Place it in the `### Added` group (create the group right under the `## [Unreleased]` line if it does not already exist):

```markdown
## [Unreleased]

### Added
- Web chat: a non-interactive "Slack" pill in the `/chat` sidebar marks sessions that originated from Slack (`slack_dm` / `slack_thread`), and `/chat?session=<id>` now deep-links straight into a session on page load. Both are client-side renders that degrade gracefully on older servers; the deep link is a one-shot, RBAC-guarded by the existing session-scoped endpoints (an unknown/forbidden id lands on an empty chat with an error status rather than leaking data).
```

(If `## [Unreleased]` already has an `### Added` group, append the bullet to it instead of creating a second group.)

- [ ] Run the changelog-adjacent guard if present and the touched suites one final time: `.venv/bin/pytest tests/test_chat_web_deeplink.py tests/test_chat_surface_badge.py tests/test_chat_web_route.py tests/test_design_system_contract.py -q` — green.
- [ ] Commit:

```bash
git add CHANGELOG.md
git commit -m "changelog: Slack surface pill + /chat deep link"
```

---

## Notes for the implementer (load-bearing facts verified against current `main`)

- `GET /api/chat/sessions` (`app/api/chat.py:97`, `list_sessions`) **already** emits `"surface": s.surface.value` (line 106). The `Surface` enum (`app/chat/types.py:10`) already defines `WEB = "web"`, `SLACK_DM = "slack_dm"`, `SLACK_THREAD = "slack_thread"`. **No server-side change is needed to expose `surface`** — the badge is pure JS over data already on the wire.
- `chat_page()` lives at `app/web/router.py:3166`. It already reads other query params elsewhere in the router (e.g. `request.query_params.get("source", ...)`), so `request.query_params.get("session")` is the established pattern.
- **Spec-vs-reality (no action — flagging the literal mismatch):** Spec §5.1 names the stylesheet `style-custom.css`, but the chat-specific stylesheet is `app/web/static/css/chat.css` (loaded by `chat.html`'s `head_extra` block, line 20). This plan uses `chat.css` throughout — that is the real file. chat.css already uses `var(--ds-*)` tokens (e.g. `var(--ds-accent-info-bg)`) — the badge reuses those. `chat.css` is inside `_CANONICAL_CSS` in `tests/test_design_system_contract.py` (the `css/*.css` glob), so any class it defines participates in the macro-coverage guard, but `.cloud-chat-surface-badge` is emitted only from JS, not from a `_components.html` macro, so it is not subject to `test_component_macros_emit_only_classes_with_css_rules`.
- There is **no existing repo-wide "no raw hex in chat.css" test** — `test_swept_templates_use_no_raw_hex` only covers three named HTML templates. Task 3's `test_chat_css_surface_badge_uses_only_ds_tokens` is the guard that enforces the design-token-only rule for the new CSS, scoped to the badge block.
- The boot IIFE (`chat.js:1666`) `await loadSidebar()` populates `_sessionsCache` before `_maybeOpenInitialSession()` runs, so `openSession(id)` can resolve the title from cache the same way a sidebar click does.
- **Deep-link failure behaviour (verified against `openSession` at chat.js:361 — NOT a clean no-op):** `openSession(chatId)` sets `currentChatId = chatId` **immediately** (line 363), clears `#chat-messages` (line 369), and sets the thread title from the sidebar cache — for an unknown id the cache miss falls back to "Untitled chat" (line 368). It then awaits `GET /api/chat/sessions/{id}/messages` and mints a ticket via `POST /sessions/{id}/ticket`; an unknown/forbidden id makes those calls fail their RBAC guards and surface `Could not load history: …` / `Could not resume chat: …` via `setStatus`. **Net effect:** a bad deep link leaves the UI on an empty "Untitled chat" with a visible error status — it does **not** literally no-op the way spec §5.2 phrases it. This is functionally non-crashing and RBAC-safe (no data leaks; ownership is enforced server-side by the session-scoped endpoints), which is the property the design relies on. The plan does not attempt to make `openSession` cleanly bail on a bad id — that hardening, if wanted, is out of scope for this phase.
- `document.body.dataset.initialSession` maps to the `data-initial-session` attribute emitted in `chat.html`'s `body_attrs` block (line 17). The module-level read happens at script parse time; since `chat.js` is loaded as `<script type="module">` at the end of `{% block scripts %}` (chat.html:212), `document.body` is fully parsed by then.
