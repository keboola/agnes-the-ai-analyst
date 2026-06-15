# Authoring Agents — Slice 0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove the "profiled chat session" mechanism end-to-end and ship a minimal, demoable **Data-package builder** web page with a deterministic browser E2E that records video — with **zero schema migration**.

**Architecture:** Reuse the E2B chat runtime as the backend brain. Add an optional `profile` to chat-session creation that is threaded to the spawn path and materialized as a profile-specific `CLAUDE.md` + read-only knowledge skill written into the per-session workdir (picked up by the runner's existing `setting_sources`). `profile` is a **spawn-time parameter, not a persisted column** (so no migration). A new admin-only web page (`/admin/studio/data-package`) hosts a minimal builder form + an assistant panel that opens a profiled session, and a "Create package" action that calls the existing `/api/admin/data-packages` endpoints. A Playwright E2E boots the docker-compose stack in fake-agent mode and records video of the builder creating a package.

**Tech Stack:** FastAPI, Jinja2 + vanilla ES6 JS, the `--ds-*` design system, the existing `app/chat/` runtime, Playwright + Chromium, docker-compose E2E harness.

**Scope guard:** No `authoring_suggestions` queue, no dual-backend repo, no `ResourceType` change, no migration. Admin caller only (RBAC is god-mode → no role-filtered toolset yet). The live-agent (real-LLM) E2E is out of scope (needs `ANTHROPIC_API_KEY` + E2B); this slice's E2E runs in deterministic fake-agent mode.

---

## File Structure

- `app/chat/profiles.py` *(create)* — the profile registry: `{slug: ChatProfile}` where `ChatProfile` carries the persona `CLAUDE.md` text + a read-only knowledge skill (name + body). One entry: `data-package-builder`.
- `app/chat/workdir.py` *(modify)* — `prepare_session_dir` gains an optional `profile: ChatProfile | None`; when set, it writes a real `CLAUDE.md` (overriding the workspace symlink) and a `.claude/skills/<skill-name>/SKILL.md` into the session dir.
- `app/chat/manager.py` *(modify)* — `create_session(..., profile: str | None = None)`; resolve the slug via the registry, pass the resolved `ChatProfile` to `prepare_session_dir`.
- `app/api/chat.py` *(modify)* — `CreateSessionBody` gains `profile: Optional[str] = None`; passed to `create_session`. Unknown slug → 400.
- `app/web/router.py` *(modify)* — add `GET /admin/studio/data-package` (admin-gated) rendering the new template.
- `app/web/templates/admin_studio_data_package.html` *(create)* — minimal builder page (extends `base_ds.html`): form (name/slug/description) + assistant panel + "Create package" button.
- `app/web/static/js/studio_data_package.js` *(create)* — opens a profiled chat session, streams into the assistant panel (reuse `chat.js` frame handling), wires the Create button to `POST /api/admin/data-packages`.
- `tests/test_chat_profiles.py` *(create)* — unit tests for the registry + workdir materialization.
- `tests/test_chat_api.py` *(modify)* — `profile` accepted/validated on `POST /api/chat/sessions`.
- `tests/test_web_studio_data_package.py` *(create)* — route renders, admin-gated.
- `tests/e2e/test_studio_web.py` *(create)* — Playwright builder E2E with video.
- `CHANGELOG.md` *(modify)* — `[Unreleased]` bullet.

---

## Task A: Profile registry (`app/chat/profiles.py`)

**Files:**
- Create: `app/chat/profiles.py`
- Test: `tests/test_chat_profiles.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_chat_profiles.py
from app.chat.profiles import get_profile, ChatProfile


def test_known_profile_resolves():
    p = get_profile("data-package-builder")
    assert isinstance(p, ChatProfile)
    assert p.slug == "data-package-builder"
    assert "data package" in p.claude_md.lower()
    assert p.skill_name and p.skill_body
    # persona must steer the agent at the existing admin endpoints
    assert "/api/admin/data-packages" in p.skill_body


def test_unknown_profile_returns_none():
    assert get_profile("does-not-exist") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_chat_profiles.py -v`
Expected: FAIL — `ModuleNotFoundError: app.chat.profiles`

- [ ] **Step 3: Write minimal implementation**

```python
# app/chat/profiles.py
"""Authoring-agent chat profiles.

A profile shapes a chat session into a specialized authoring assistant by
supplying (a) a persona ``CLAUDE.md`` that replaces the generic analyst data
rails, and (b) a read-only knowledge skill describing how the target domain
works in Agnes. Profiles are spawn-time only — they materialize into the
per-session workdir (see ``WorkdirManager.prepare_session_dir``) and are not
persisted, so adding one needs no schema migration.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChatProfile:
    slug: str
    claude_md: str          # replaces the session CLAUDE.md
    skill_name: str         # .claude/skills/<skill_name>/SKILL.md
    skill_body: str         # full SKILL.md (frontmatter + body)


_DATA_PACKAGE_BUILDER = ChatProfile(
    slug="data-package-builder",
    claude_md=(
        "# Data Package Builder\n\n"
        "You help an admin assemble a **data package** — a curated bundle of "
        "tables (and later metrics) granted to a user group — in Agnes.\n\n"
        "Rules:\n"
        "- Ground every suggestion in the instance's real state: run `agnes "
        "catalog --json` to see available tables before proposing any.\n"
        "- Check for an existing near-duplicate package before proposing a new "
        "one; suggest editing it instead if found.\n"
        "- Propose; never claim a package is created until the admin clicks "
        "Create in the builder UI.\n"
        "- Use the `agnes-data-package` skill for the exact model and endpoints.\n"
    ),
    skill_name="agnes-data-package",
    skill_body=(
        "---\n"
        "name: agnes-data-package\n"
        "description: How data packages work in Agnes — model, the catalog, "
        "and the admin endpoints used to assemble and grant one.\n"
        "---\n\n"
        "# Data packages in Agnes\n\n"
        "A data package = `data_packages` row + `data_package_tables` (M:N to "
        "`table_registry`) + a `resource_grant` to a user group.\n\n"
        "## Read the real state first\n"
        "- `agnes catalog --json` — available tables (id, query_mode, size).\n"
        "- `agnes schema <table_id>` — columns + types.\n\n"
        "## Assemble (admin endpoints)\n"
        "- `POST /api/admin/data-packages` — create `{name, slug, description}`.\n"
        "- `POST /api/admin/data-packages/{id}/tables` — add a table.\n"
        "- `POST /api/admin/grants` — grant the package to a group.\n\n"
        "Local tables (`query_mode` local/materialized) sync to analysts via "
        "`agnes pull`; `remote` tables stay server-side.\n"
    ),
)

_PROFILES: dict[str, ChatProfile] = {
    _DATA_PACKAGE_BUILDER.slug: _DATA_PACKAGE_BUILDER,
}


def get_profile(slug: str) -> ChatProfile | None:
    """Return the profile for ``slug`` or ``None`` if unknown."""
    return _PROFILES.get(slug)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_chat_profiles.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add app/chat/profiles.py tests/test_chat_profiles.py
git commit -m "feat(chat): authoring-agent profile registry"
```

---

## Task B: Materialize a profile into the session workdir (`app/chat/workdir.py`)

**Files:**
- Modify: `app/chat/workdir.py` (`prepare_session_dir`)
- Test: `tests/test_chat_profiles.py` (extend)

- [ ] **Step 1: Write the failing test** (append to `tests/test_chat_profiles.py`)

```python
def test_prepare_session_dir_materializes_profile(tmp_path, monkeypatch):
    from app.chat.workdir import WorkdirManager
    from app.chat.profiles import get_profile

    mgr = WorkdirManager(data_dir=tmp_path)
    # a minimal user workspace must exist for the symlink step
    ws = mgr.user_workspace("admin@example.com")
    (ws / ".claude").mkdir(parents=True, exist_ok=True)
    (ws / "CLAUDE.md").write_text("generic rails", encoding="utf-8")

    sdir = mgr.prepare_session_dir(
        "admin@example.com", "chat-xyz", profile=get_profile("data-package-builder")
    )

    claude_md = (sdir / "CLAUDE.md").read_text(encoding="utf-8")
    assert "Data Package Builder" in claude_md          # profile, not "generic rails"
    skill = sdir / ".claude" / "skills" / "agnes-data-package" / "SKILL.md"
    assert skill.exists()
    assert "data_packages" in skill.read_text(encoding="utf-8")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_chat_profiles.py::test_prepare_session_dir_materializes_profile -v`
Expected: FAIL — `prepare_session_dir() got an unexpected keyword argument 'profile'`

- [ ] **Step 3: Implement** — extend `prepare_session_dir` signature + materialization.

In `app/chat/workdir.py`, change the signature (currently `def prepare_session_dir(self, user_email, chat_id, *, include_personal_override=True)`) to add `profile: "ChatProfile | None" = None` (import `ChatProfile` under `TYPE_CHECKING` to avoid a cycle), and after the existing symlink loop + `(sdir / "work").mkdir(...)`, before `return sdir`, insert:

```python
        if profile is not None:
            # Profile overrides the symlinked workspace CLAUDE.md with a
            # persona, and drops a read-only knowledge skill into the session
            # .claude/skills. We write real files into the session dir (the
            # symlink, if any, is replaced) so the runner's setting_sources
            # picks the profile up without touching the shared workspace.
            cmd = sdir / "CLAUDE.md"
            if cmd.is_symlink() or cmd.exists():
                cmd.unlink()
            cmd.write_text(profile.claude_md, encoding="utf-8")
            skill_dir = sdir / ".claude" / "skills" / profile.skill_name
            skill_dir.mkdir(parents=True, exist_ok=True)
            (skill_dir / "SKILL.md").write_text(profile.skill_body, encoding="utf-8")
```

Note: the `.claude` entry is a symlink to the shared workspace; writing into `sdir/.claude/skills/...` would write through the symlink into the shared workspace. To avoid that, when `profile is not None`, **replace the `.claude` symlink with a copied directory** before writing the skill. Adjust the symlink loop so that for `profile is not None` the `.claude` entry is copied (shutil.copytree of the target if it exists, else mkdir) instead of symlinked. Implement that branch explicitly.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_chat_profiles.py -v`
Expected: PASS (3 passed). Verify the shared workspace `.claude` was NOT mutated (add an assertion: `assert not (ws / ".claude" / "skills" / "agnes-data-package").exists()`).

- [ ] **Step 5: Commit**

```bash
git add app/chat/workdir.py tests/test_chat_profiles.py
git commit -m "feat(chat): materialize authoring profile into session workdir"
```

---

## Task C: Thread `profile` through manager + API

**Files:**
- Modify: `app/chat/manager.py` (`create_session`), `app/api/chat.py` (`CreateSessionBody`, `create_session` route)
- Test: `tests/test_chat_api.py` (extend), `tests/test_chat_manager.py` (extend)

- [ ] **Step 1: Write the failing API test** (append to `tests/test_chat_api.py`, mirroring the existing client/admin fixtures in that file)

```python
def test_create_session_accepts_known_profile(admin_client):
    r = admin_client.post("/api/chat/sessions", json={"profile": "data-package-builder"})
    assert r.status_code == 201, r.text


def test_create_session_rejects_unknown_profile(admin_client):
    r = admin_client.post("/api/chat/sessions", json={"profile": "nope"})
    assert r.status_code == 400
    assert r.json()["detail"]["kind"] == "unknown_profile"
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_chat_api.py -k profile -v`
Expected: FAIL — 422/201 mismatch (field unknown) / no validation.

- [ ] **Step 3: Implement**

In `app/api/chat.py`: add `profile: Optional[str] = None` to `CreateSessionBody`; in the `create_session` route, before calling `mgr.create_session`, validate:

```python
    if body.profile is not None and get_profile(body.profile) is None:
        raise HTTPException(status_code=400, detail={"kind": "unknown_profile", "hint": body.profile})
```

(import `from app.chat.profiles import get_profile`) and pass `profile=body.profile` into `mgr.create_session(...)`.

In `app/chat/manager.py`: add `profile: str | None = None` to `create_session`; resolve `prof = get_profile(profile) if profile else None`; thread `prof` to wherever it calls `prepare_session_dir(...)` (pass `profile=prof`). (For co-sessions, ignore profile.)

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/pytest tests/test_chat_api.py tests/test_chat_manager.py -k "profile or create_session" -v`
Expected: PASS.

- [ ] **Step 5: Run the chat regression slice**

Run: `.venv/bin/pytest tests/test_chat_api.py tests/test_chat_manager.py tests/test_chat_persistence.py -q`
Expected: PASS (no regressions from the signature change).

- [ ] **Step 6: Commit**

```bash
git add app/api/chat.py app/chat/manager.py tests/test_chat_api.py tests/test_chat_manager.py
git commit -m "feat(chat): accept and validate session profile param"
```

---

## Task D: Data-package builder web page

**Files:**
- Modify: `app/web/router.py` (new route)
- Create: `app/web/templates/admin_studio_data_package.html`, `app/web/static/js/studio_data_package.js`
- Test: `tests/test_web_studio_data_package.py`

> **Pattern to follow (read these first):** `app/web/templates/admin_mcp_sources.html` for the page shell + embedded-JS admin pattern, `app/web/static/js/chat.js` for the WebSocket frame loop to reuse in the assistant panel, and an existing admin route in `app/web/router.py` (e.g. the `/admin/corporate-memory` handler) for the `require_admin` + template-render shape. Use `base_ds.html`, `var(--ds-*)` tokens only, CSS in `{% block head_extra %}` (no inline body CSS), `ds.*` macros for buttons/inputs.

- [ ] **Step 1: Write the failing route test**

```python
# tests/test_web_studio_data_package.py
def test_studio_page_requires_admin(client_non_admin):
    r = client_non_admin.get("/admin/studio/data-package")
    assert r.status_code in (302, 403)


def test_studio_page_renders_for_admin(admin_client):
    r = admin_client.get("/admin/studio/data-package")
    assert r.status_code == 200
    assert "data-package-builder" in r.text          # the profile slug wired into JS
    assert 'id="studio-create"' in r.text             # the Create button
```

(Use the same admin/non-admin client fixtures the other `tests/test_web_*.py` files use.)

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_web_studio_data_package.py -v`
Expected: FAIL — 404 (route absent).

- [ ] **Step 3: Implement the route** in `app/web/router.py` (mirror an existing admin HTML route):

```python
@router.get("/admin/studio/data-package", response_class=HTMLResponse)
async def studio_data_package(request: Request, user: dict = Depends(require_admin)):
    return templates.TemplateResponse(
        "admin_studio_data_package.html",
        {"request": request, "profile_slug": "data-package-builder"},
    )
```

- [ ] **Step 4: Implement the template** `app/web/templates/admin_studio_data_package.html`:
  - Extends `base_ds.html`.
  - Two-column layout (reuse the `.ax-page`/`.ax-card` or `.obs-panel` split styling — copy the minimal CSS into `{% block head_extra %}` using `--ds-*` tokens).
  - Left card "Draft": `ds`-styled inputs for `name`, `slug`, `description`; a `ds.button(primary)` with `id="studio-create"` labeled "Create package".
  - Right card "Assistant": a `#studio-stream` log region (`aria-live="polite"`) + a message input.
  - `{% block scripts %}`: `<script type="module" src="/static/js/studio_data_package.js"></script>` and a `<script>window.STUDIO_PROFILE="{{ profile_slug }}";</script>`.

- [ ] **Step 5: Implement the JS** `app/web/static/js/studio_data_package.js`:
  - On load: `POST /api/chat/sessions {profile: window.STUDIO_PROFILE}` → open the returned `ws_url`; reuse `chat.js`'s frame switch (token/tool_call/assistant_message) to append into `#studio-stream`.
  - Create button → `POST /api/admin/data-packages` with `{name, slug, description}` from the form; on 201 show a success toast (`appToast` from `app.js`) and the new package id.
  - Use the `api()` fetch helper pattern from `chat.js` (`credentials: "same-origin"`).

- [ ] **Step 6: Run the route test**

Run: `.venv/bin/pytest tests/test_web_studio_data_package.py -v`
Expected: PASS.

- [ ] **Step 7: Design-system contract check**

Run: `.venv/bin/pytest tests/test_design_system_contract.py -q`
Expected: PASS (no `base.html`, no raw hex, no `var(--primary)`, no inline body CSS in the new template).

- [ ] **Step 8: Commit**

```bash
git add app/web/router.py app/web/templates/admin_studio_data_package.html app/web/static/js/studio_data_package.js tests/test_web_studio_data_package.py
git commit -m "feat(web): data-package builder studio page (admin)"
```

---

## Task E: Deterministic browser E2E with video

**Files:**
- Create: `tests/e2e/test_studio_web.py`
- Modify: `tests/e2e/test_chat_web.py` is the reference; reuse its `chrome`/`page` fixtures by importing or duplicating the minimal context fixture **with video recording enabled**.

> **Prereqs (run once in the worktree):**
> ```bash
> python3 -m venv .venv && . .venv/bin/activate
> uv pip install -e ".[dev]"
> playwright install chromium --with-deps
> ```

- [ ] **Step 1: Write the E2E test** (deterministic — fake agent; the assertion does not depend on the LLM)

```python
# tests/e2e/test_studio_web.py
"""Browser E2E for the data-package builder studio page.

Deterministic: the Create action calls the real /api/admin/data-packages
endpoint, so the assertion never depends on the LLM. Runs against the
docker-compose stack in fake-agent mode. Records video to
tests/e2e/_videos/.

Gated: AGNES_E2E=1 + docker + Playwright/Chromium (see conftest).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

try:
    from playwright.sync_api import sync_playwright as _spw
    from playwright.sync_api import Error as _PwErr
    _PW = True
except ImportError:
    _PW = False

_VIDEO_DIR = Path(__file__).parent / "_videos"


@pytest.fixture
def video_page(docker_e2e_agnes):
    if not _PW:
        pytest.skip("playwright not installed")
    if not os.environ.get("AGNES_E2E"):
        pytest.skip("set AGNES_E2E=1")
    _VIDEO_DIR.mkdir(exist_ok=True)
    pw = _spw().start()
    try:
        browser = pw.chromium.launch()
    except _PwErr as exc:
        pw.stop()
        pytest.skip(f"chromium missing: {exc}")
    ctx = browser.new_context(record_video_dir=str(_VIDEO_DIR), record_video_size={"width": 1280, "height": 800})
    page = ctx.new_page()
    yield page, docker_e2e_agnes
    ctx.close()          # finalizes the .webm
    browser.close()
    pw.stop()


def test_builder_creates_data_package(video_page):
    page, base = video_page
    # Auth: reuse the e2e seed-admin login helper used by test_chat_web.py
    # (see that file's login fixture/helper; call it here).
    page.goto(f"{base}/admin/studio/data-package")
    page.fill("#dp-name", "E2E Finance")
    page.fill("#dp-slug", "e2e-finance")
    page.fill("#dp-description", "Created by the studio E2E")
    page.click("#studio-create")
    page.wait_for_selector("text=Created", timeout=10_000)
    # verify via API that the package now exists
    resp = page.request.get(f"{base}/api/admin/data-packages")
    assert resp.ok
    assert any(p["slug"] == "e2e-finance" for p in resp.json())
```

- [ ] **Step 2: Run with the stack (fake agent, dummy key)**

```bash
export AGNES_E2E=1 AGNES_E2E_FAKE_AGENT=1 ANTHROPIC_API_KEY=dummy-e2e
.venv/bin/pytest tests/e2e/test_studio_web.py -v
```
Expected: PASS; a `.webm` appears under `tests/e2e/_videos/`.

- [ ] **Step 3: Confirm the video artifact**

Run: `ls -la tests/e2e/_videos/*.webm`
Expected: at least one non-empty `.webm`.

- [ ] **Step 4: Commit** (do NOT commit the video; add `_videos/` to `.gitignore`)

```bash
echo "tests/e2e/_videos/" >> .gitignore
git add tests/e2e/test_studio_web.py .gitignore
git commit -m "test(e2e): data-package builder browser E2E with video capture"
```

---

## Task F: CHANGELOG + full suite

- [ ] **Step 1: Add CHANGELOG bullet** under `## [Unreleased]` → `### Added`:

```
- Authoring agents (Slice 0): profiled chat sessions + a Data-package builder studio page (`/admin/studio/data-package`, admin-only).
```

- [ ] **Step 2: Run the full suite (what CI runs)**

Run: `.venv/bin/pytest tests/ --tb=short -n auto -q`
Expected: PASS (E2E auto-skips without `AGNES_E2E=1`).

- [ ] **Step 3: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs: changelog for authoring-agents slice 0"
```

---

## Self-review notes

- **Spec coverage:** implements §3.1 (profile via workdir CLAUDE.md, no migration), §4.2 (data-package agent, minimal), §13 (builder page + assistant panel, design-system contract). Defers: role-filtered toolsets (§5), suggestion queue (§5/§6), other three agents, drift (§10) — all explicitly out of Slice 0 scope per §9.
- **No migration:** `profile` is spawn-time only; no `chat_sessions` column, so no DuckDB↔PG ladder work in this slice. (When a future slice persists profile for resume, that triggers the §12 migration checklist.)
- **Admin-only:** the route is `require_admin`; the agent's mutations hit `require_admin` endpoints — RBAC is satisfied by existing gates, so no role-filtered toolset yet (consistent with the §5 finding that server-side gates are the real boundary).
- **Live-agent E2E** (real Claude driving the builder, marked `real_llm`) is intentionally absent — needs `ANTHROPIC_API_KEY` + E2B. The shipped E2E is deterministic and asserts package creation via the real endpoint.
