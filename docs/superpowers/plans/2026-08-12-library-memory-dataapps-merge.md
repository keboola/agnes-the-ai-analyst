# Library Memory/Data-apps Merge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Under the rail chrome, the Library becomes the one browse surface for memory domains and hosted data apps; the standalone `/corporate-memory` and `/apps` pages redirect there.

**Architecture:** All changes are rail-only by construction: the `/library` route forks to `library_legacy.html` before the sections pipeline runs, and both redirects are keyed on `get_ui_layout() == "rail"`. The Library sections pipeline in `app/web/router.py` gains a `data_app` section and enriches `memory_domain` rows; the interim rail nav rows from PR #1276 come out.

**Tech Stack:** FastAPI + Jinja2 templates, DuckDB repositories, pytest.

**Spec:** `docs/superpowers/specs/2026-08-12-library-memory-dataapps-merge-design.md`

## Global Constraints

- Default (topnav) chrome renders byte-identically — never touch `library_legacy.html`, `corporate_memory_legacy.html`, `data_apps.html` rendering for topnav; `tests/test_ui_layout_theme.py::TestDefaultContentParity` must stay green.
- Everything data-apps is gated on `_data_apps_nav_enabled()` (router) / `data_apps_enabled()` (Jinja global).
- Redirects are 302, never 308 (layout flips must not be cached).
- No customer-specific tokens anywhere (public repo).
- Run tests from the repo root with `.venv/bin/pytest` (worktree symlinks the main venv; cwd resolves `app`/`src` imports).
- Branch: `zs/library-memory-dataapps-merge` (stacked on `zs/connections-new-ui`, PR #1276).

---

### Task 1: Data apps band in the Library sections pipeline

**Files:**
- Modify: `app/web/router.py` — sections pipeline (`library_page`): rows after the store-entities block (~line 3450), `_SECTION_ORDER`/`_SECTION_LABELS`/`_SECTION_HINTS`/`_SECTION_KINDS` (~lines 3591–3671), `_SECTION_SOON`/`_SECTION_SOON_TIP` (remove the `files` entry, ~3648)
- Modify: `app/web/templates/macros/_catalog_card.html` — `kind_glyph` macro (~line 57): new `app` glyph
- Modify: `app/web/static/js/catalog_card.js` — `KIND_GLYPH` map (kept in lockstep with the macro per its comment)
- Test: `tests/test_web_library_data_apps.py` (new)

**Interfaces:**
- Consumes: `data_apps_repo().list(include_drafts=False)`, `app.api.data_apps._can_view(user, row)`, `app.api.data_apps._serialize(row, cfg)`, `get_data_apps_config()` (already imported in the router), `_library_row_base(...)` (router ~2598), `_data_apps_nav_enabled()` (router :311).
- Produces: Library rows with `type_key="data_app"`, `href="/apps/detail/{slug}"`, section key `data_app` labeled "Data apps". Task 3's redirect target `/library?section=data_app` depends on this section key.

- [ ] **Step 1: Write the failing tests**

```python
"""tests/test_web_library_data_apps.py — the Library's Data apps band.

Rail-only by construction (the /library route serves library_legacy.html to
topnav before the sections pipeline runs); these tests pin the rail render.
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _seed_app(slug="revenue-dash", name="Revenue dashboard", owner_id="admin1"):
    from src.db import get_system_db
    from src.repositories.data_apps import DataAppsRepository

    conn = get_system_db()
    try:
        DataAppsRepository(conn).create(
            slug=slug, name=name, owner_user_id=owner_id,
            description="Streamlit revenue overview",
        )
    finally:
        conn.close()


def test_band_lists_visible_app_under_rail(seeded_app, monkeypatch):
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
    monkeypatch.setenv("AGNES_DATA_APPS_ENABLED", "1")
    _seed_app()
    c = seeded_app["client"]
    resp = c.get("/library", headers=_auth(seeded_app["admin_token"]))
    assert resp.status_code == 200
    body = resp.text
    assert "Data apps" in body
    assert 'href="/apps/detail/revenue-dash"' in body
    assert "Revenue dashboard" in body


def test_band_absent_when_feature_disabled(seeded_app, monkeypatch):
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
    monkeypatch.delenv("AGNES_DATA_APPS_ENABLED", raising=False)
    _seed_app(slug="hidden-app", name="Hidden app")
    c = seeded_app["client"]
    resp = c.get("/library", headers=_auth(seeded_app["admin_token"]))
    assert resp.status_code == 200
    assert 'href="/apps/detail/hidden-app"' not in resp.text


def test_non_owner_without_grant_sees_no_app_row(seeded_app, monkeypatch):
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
    monkeypatch.setenv("AGNES_DATA_APPS_ENABLED", "1")
    _seed_app(slug="private-app", name="Private app", owner_id="admin1")
    c = seeded_app["client"]
    resp = c.get("/library", headers=_auth(seeded_app["analyst_token"]))
    assert resp.status_code == 200
    assert 'href="/apps/detail/private-app"' not in resp.text


def test_soon_badge_gone_from_files_band(seeded_app, monkeypatch):
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
    c = seeded_app["client"]
    resp = c.get("/library", headers=_auth(seeded_app["admin_token"]))
    assert resp.status_code == 200
    assert "Data apps coming soon" not in resp.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_web_library_data_apps.py -q --tb=short`
Expected: FAIL — `"Data apps" in body` (no band), `"Data apps coming soon" not in` (badge still present).

- [ ] **Step 3: Implement the band**

In `app/web/router.py`, after the store-entities block (the loop ending near line 3450, `origin="installed"`), still inside the rail branch of `library_page`, add:

```python
    # ── Hosted data apps ───────────────────────────────────────────────
    # Same visibility set as the /apps page (data_apps_list_page): the
    # caller's own apps plus apps granted to their groups, minus drafts and
    # `linked_hidden` rows. No stack membership — data-app access is
    # grant-driven, not stack-driven — and no lifecycle actions here; the
    # detail page owns start/stop/logs. Under the rail chrome this band is
    # the ONLY way to the apps inventory (/apps redirects here, see the
    # data_apps_list_page handler), so removing it strands the surface.
    if _data_apps_nav_enabled():
        from app.api.data_apps import _can_view as _da_can_view
        from app.api.data_apps import _serialize as _da_serialize
        from src.repositories import data_apps_repo, users_repo

        try:
            _da_cfg = get_data_apps_config()
            _da_users = users_repo()
            for da in data_apps_repo().list(include_drafts=False):
                if da.get("state") == "linked_hidden" or not _da_can_view(user, da):
                    continue
                _da = _da_serialize(da, _da_cfg)
                _da_mine = da["owner_user_id"] == uid
                _da_owner = _da_users.get_by_id(da["owner_user_id"]) or {}
                _da_meta = " · ".join(
                    b for b in (_da.get("state") or "", "linked" if _da.get("kind") == "linked" else "") if b
                )
                items.append(
                    _library_row_base(
                        item_id=da["slug"],
                        kind="library",
                        title=da.get("name") or da["slug"],
                        description=_da.get("effective_description") or "",
                        href=f"/apps/detail/{da['slug']}",
                        glyph="app",
                        type_key="data_app",
                        type_label="Data app",
                        origin="built" if _da_mine else "granted",
                        origin_label="Built here" if _da_mine else "Shared with you",
                        added_iso=None,
                        owner_label="You" if _da_mine else (_da_owner.get("email") or da["owner_user_id"]),
                        ownership="mine" if _da_mine else "shared_with_me",
                        visibility="private" if _da_mine else "shared",
                        visibility_label="Yours" if _da_mine else "Shared with you",
                        meta_text=_da_meta,
                        share_type=None,
                        requirement="optional",
                        tags=[],
                        owner_key="me" if _da_mine else (da["owner_user_id"] or "workspace"),
                    )
                )
        except Exception as e:
            logger.warning("/library: could not list data apps: %s", e)
```

Then wire the section vocabulary (all in `library_page`):

```python
    _SECTION_ORDER = [
        "data_package",
        "plugin",
        "skill",
        "agent",
        "recipe",
        # Loose files + collections-as-folders.
        "files",
        "memory_domain",
        "data_app",
    ]
```

```python
        # in _SECTION_LABELS
        "data_app": "Data apps",
```

```python
        # in _SECTION_HINTS
        "data_app": "Hosted apps running next to your data.",
```

```python
        # in _SECTION_KINDS — neutral library accent (the data-app detail page
        # carries no kind colour to match), with its own glyph.
        "data_app": ("library", "app"),
```

Remove the fulfilled promise (delete the entries, keep the empty dicts if
nothing else remains — `_SECTION_SOON.get(key, "")` tolerates both):

```python
    _SECTION_SOON: dict[str, str] = {}
    _SECTION_SOON_TIP: dict[str, str] = {}
```

In `app/web/templates/macros/_catalog_card.html`, add to `kind_glyph` (next to the other `elif` arms):

```html
  {%- elif kind == 'app' -%}
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><rect x="4" y="4" width="7" height="7" rx="1.5" stroke="currentColor" stroke-width="1.7"/><rect x="13" y="4" width="7" height="7" rx="1.5" stroke="currentColor" stroke-width="1.7"/><rect x="4" y="13" width="7" height="7" rx="1.5" stroke="currentColor" stroke-width="1.7"/><rect x="13" y="13" width="7" height="7" rx="1.5" stroke="currentColor" stroke-width="1.7"/></svg>
```

In `app/web/static/js/catalog_card.js`, add the same four-squares SVG under key `app` to `KIND_GLYPH` (the file's comment demands lockstep with the macro; copy the exact `<svg>` markup above into the map following the existing entries' string format).

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_web_library_data_apps.py tests/test_web_library_sharing.py tests/test_web_library_store_entities.py -q --tb=short`
Expected: PASS (new file green; the two existing library suites prove no regression).

- [ ] **Step 5: Commit**

```bash
git add app/web/router.py app/web/templates/macros/_catalog_card.html app/web/static/js/catalog_card.js tests/test_web_library_data_apps.py
git commit -m "feat(library): Data apps band — the promised kind lands as its own section"
```

---

### Task 2: Memory rows carry counts; empty domains hide

**Files:**
- Modify: `app/web/router.py` — the granted data-package/memory-domain loop in `library_page` (~lines 3253–3330: the `for rt, type_key, ... in ((ResourceType.DATA_PACKAGE, ...), (ResourceType.MEMORY_DOMAIN, ...))` block)
- Test: `tests/test_web_library_memory_band.py` (new)

**Interfaces:**
- Consumes: `memory_domains_repo().list_items_of_domain(domain_id, limit=10000)` (the same call `/corporate-memory` uses), the existing `dom_slugs` map already built in this block.
- Produces: memory rows whose `meta_text` is `"N items · M required"` (or `"N items"` when `M == 0`); rows for empty non-required domains are absent.

- [ ] **Step 1: Write the failing tests**

```python
"""tests/test_web_library_memory_band.py — the Library Memory band absorbs
what only /corporate-memory cards had: item counts and empty-domain hiding.
Rail-only (topnav serves library_legacy.html before this pipeline runs).

Seed helpers are verbatim copies from tests/test_web_memory_domain_detail.py
— same schema, same junction table.
"""

from __future__ import annotations

import uuid


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _make_domain(slug: str, name: str) -> str:
    from src.db import get_system_db
    from src.repositories.memory_domains import MemoryDomainsRepository

    conn = get_system_db()
    try:
        return MemoryDomainsRepository(conn).create(
            slug=slug,
            name=name,
            description=f"{name} desc",
            icon="🎯",
            color="#dcfce7",
            created_by="test",
        )
    finally:
        conn.close()


def _make_item(item_id: str, title: str, domain_id: str, is_required: bool = False):
    from src.db import get_system_db
    from src.repositories.knowledge import KnowledgeRepository

    conn = get_system_db()
    try:
        KnowledgeRepository(conn).create(
            id=item_id,
            title=title,
            content=f"# {title}\n\nbody",
            category="workflow",
            status="approved",
            is_required=is_required,
            source_user="contrib@example.com",
        )
        conn.execute(
            "INSERT INTO knowledge_item_domains(item_id, domain_id, added_by) VALUES (?, ?, 'test')",
            [item_id, domain_id],
        )
    finally:
        conn.close()


def _grant_domain(group_name: str, domain_id: str, requirement: str = "available"):
    from src.db import get_system_db

    conn = get_system_db()
    try:
        gid_row = conn.execute("SELECT id FROM user_groups WHERE name = ?", [group_name]).fetchone()
        if not gid_row:
            return
        conn.execute(
            "INSERT INTO resource_grants(id, group_id, resource_type, resource_id, "
            "requirement, assigned_at, assigned_by) "
            "VALUES (?, ?, 'memory_domain', ?, ?, CURRENT_TIMESTAMP, 'test')",
            [str(uuid.uuid4()), gid_row[0], domain_id, requirement],
        )
    finally:
        conn.close()


def test_memory_row_meta_carries_counts(seeded_app, monkeypatch):
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
    dom = _make_domain("lib-ops", "Lib Ops")
    _make_item("lib_ops_1", "Runbook", dom)
    _make_item("lib_ops_2", "Escalation", dom, is_required=True)
    _grant_domain("Everyone", dom)
    c = seeded_app["client"]
    resp = c.get("/library", headers=_auth(seeded_app["analyst_token"]))
    assert resp.status_code == 200
    body = resp.text
    assert "Lib Ops" in body
    assert "2 items · 1 required" in body


def test_empty_optional_domain_hidden(seeded_app, monkeypatch):
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
    empty = _make_domain("lib-empty", "Lib Empty")
    _grant_domain("Everyone", empty)
    c = seeded_app["client"]
    resp = c.get("/library", headers=_auth(seeded_app["analyst_token"]))
    assert resp.status_code == 200
    assert "Lib Empty" not in resp.text


def test_empty_required_domain_stays_visible(seeded_app, monkeypatch):
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
    dom = _make_domain("lib-empty-req", "Lib Empty Required")
    _grant_domain("Everyone", dom, requirement="required")
    c = seeded_app["client"]
    resp = c.get("/library", headers=_auth(seeded_app["analyst_token"]))
    assert resp.status_code == 200
    assert "Lib Empty Required" in resp.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_web_library_memory_band.py -q --tb=short`
Expected: FAIL — meta shows the domain category (or nothing), not counts; the empty domain renders.

- [ ] **Step 3: Implement counts + hiding**

In the granted loop in `library_page`, before the `for rt, type_key, ...` loop, build the per-domain meta once (mirror of `/corporate-memory`'s `dom_meta`):

```python
    # Per-domain item/required counts — the same numbers the standalone
    # /corporate-memory cards carry. Built once; the memory band's meta and
    # its empty-domain rule (below) both read it.
    dom_counts: dict[str, tuple[int, int]] = {}
    try:
        _dom_repo = memory_domains_repo()
        for _d in _dom_repo.list(limit=100000):
            _sums = _dom_repo.list_items_of_domain(_d["id"], limit=10000)
            dom_counts[_d["id"]] = (len(_sums), sum(1 for s in _sums if s.get("is_required")))
    except Exception as e:
        logger.warning("/library: could not count memory-domain items: %s", e)
```

Inside the loop's `memory_domain` branch (the `else:` arm that builds `href = f"/memory/d/{slug}?source=library" ...`), add before `_add_shared_row`:

```python
                    n_items, n_required = dom_counts.get(e.id, (0, 0))
                    # A domain with nothing in it has nothing to opt into —
                    # same rule as the standalone page (_has_content): hidden
                    # unless the mandate itself is required. Admins manage
                    # empty placeholders at /admin/corporate-memory#domains.
                    if n_items == 0 and e.requirement != "required":
                        continue
                    mem_meta = f"{n_items} item{'s' if n_items != 1 else ''}"
                    if n_required:
                        mem_meta += f" · {n_required} required"
```

and pass `meta_text=(mem_meta if type_key == "memory_domain" else (e.category or ""))` — concretely: keep the data_package arm untouched, and in the shared `_add_shared_row(...)` call switch the argument to a variable computed per-branch (`meta_text=row_meta`), where the data_package arm sets `row_meta = e.category or ""` and the memory arm sets `row_meta = mem_meta`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_web_library_memory_band.py tests/test_web_library_sharing.py -q --tb=short`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/web/router.py tests/test_web_library_memory_band.py
git commit -m "feat(library): memory rows carry item counts; empty optional domains hide"
```

---

### Task 3: Rail redirects for /corporate-memory and /apps

**Files:**
- Modify: `app/web/router.py` — `corporate_memory` handler (~line 4537), `data_apps_list_page` handler (~line 4843)
- Test: `tests/test_web_rail_redirects.py` (new)

**Interfaces:**
- Consumes: `get_ui_layout()` (already imported), `_data_apps_nav_enabled()`, Task 1's `data_app` section key.
- Produces: `GET /corporate-memory` → 302 `/library?section=memory_domain` (rail); `GET /apps` → 302 `/library?section=data_app` (rail AND feature enabled). Task 4's guard set relies on these.

- [ ] **Step 1: Write the failing tests**

```python
"""tests/test_web_rail_redirects.py — standalone browse pages fold into the
Library under the rail chrome; topnav serves them unchanged.

302 (not 308) so a later layout flip is not cached permanently — the same
reasoning as the /dashboard → /chat rail redirect.
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_corporate_memory_redirects_to_library_under_rail(seeded_app, monkeypatch):
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
    c = seeded_app["client"]
    resp = c.get(
        "/corporate-memory", headers=_auth(seeded_app["analyst_token"]), follow_redirects=False
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == "/library?section=memory_domain"


def test_corporate_memory_stays_a_page_under_topnav(seeded_app, monkeypatch):
    monkeypatch.delenv("AGNES_UI_LAYOUT", raising=False)
    c = seeded_app["client"]
    resp = c.get(
        "/corporate-memory", headers=_auth(seeded_app["analyst_token"]), follow_redirects=False
    )
    assert resp.status_code == 200


def test_apps_redirects_to_library_under_rail_when_enabled(seeded_app, monkeypatch):
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
    monkeypatch.setenv("AGNES_DATA_APPS_ENABLED", "1")
    c = seeded_app["client"]
    resp = c.get("/apps", headers=_auth(seeded_app["analyst_token"]), follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/library?section=data_app"


def test_apps_keeps_empty_state_under_rail_when_disabled(seeded_app, monkeypatch):
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
    monkeypatch.delenv("AGNES_DATA_APPS_ENABLED", raising=False)
    c = seeded_app["client"]
    resp = c.get("/apps", headers=_auth(seeded_app["analyst_token"]), follow_redirects=False)
    assert resp.status_code == 200


def test_apps_stays_a_page_under_topnav(seeded_app, monkeypatch):
    monkeypatch.delenv("AGNES_UI_LAYOUT", raising=False)
    monkeypatch.setenv("AGNES_DATA_APPS_ENABLED", "1")
    c = seeded_app["client"]
    resp = c.get("/apps", headers=_auth(seeded_app["analyst_token"]), follow_redirects=False)
    assert resp.status_code == 200
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_web_rail_redirects.py -q --tb=short`
Expected: the two redirect tests FAIL with 200 != 302; the other three already pass.

- [ ] **Step 3: Implement the redirects**

Top of the `corporate_memory` handler body, before any repo work:

```python
    # Rail: the Library's Memory band IS this page now (counts, add-to-stack,
    # empty-domain rule all moved there — spec 2026-08-12). 302, not 308, so
    # a later layout flip is not cached permanently. Topnav serves the frozen
    # pre-redesign page below, untouched.
    if get_ui_layout() == "rail":
        return RedirectResponse(url="/library?section=memory_domain", status_code=302)
```

Top of the `data_apps_list_page` handler body (`enabled` is computed inside — hoist the `feature_enabled(...)` call above the redirect so the gate can read it):

```python
    enabled = feature_enabled("data_apps", "enabled", env_var="AGNES_DATA_APPS_ENABLED", default=False)
    # Rail: the Library's Data apps band is the inventory now. Only when the
    # feature is on — with it off this page's explanatory empty state is the
    # better answer for a bookmark than a Library with no Data apps section.
    if enabled and get_ui_layout() == "rail":
        return RedirectResponse(url="/library?section=data_app", status_code=302)
```

(and drop the now-duplicate `enabled = feature_enabled(...)` line further down).

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_web_rail_redirects.py tests/test_corporate_memory_page.py -q --tb=short`
Expected: PASS (the standalone-page suite runs under topnav default and must not notice).

- [ ] **Step 5: Commit**

```bash
git add app/web/router.py tests/test_web_rail_redirects.py
git commit -m "feat(ui): /corporate-memory and /apps fold into the Library under rail (302)"
```

---

### Task 4: Rail rows out; guards learn the redirect set; back link returns to the Library

**Files:**
- Modify: `app/web/templates/_app_rail.html` — remove the Memory and Apps `.rail-i` rows (added by #1276), update the header IA comment
- Modify: `app/web/router.py` — `_RAIL_DETAIL_BACK["memory_domain"]` (~line 434) back to the Library section
- Modify: `tests/test_web_nav_user_parity.py` — `REDIRECTED_UNDER_RAIL` set
- Modify: `tests/test_web_memory_domain_detail.py` — back-link test returns to asserting the Library target
- Test: the two modified test files

**Interfaces:**
- Consumes: Task 3's redirects (without them, removing the rows re-orphans both pages and the parity guard rightly fails).
- Produces: final rail IA — bottom zone is Library · Agents · Admin again.

- [ ] **Step 1: Update the guard first (failing state)**

In `tests/test_web_nav_user_parity.py`, add next to `KNOWN_TOPNAV_ONLY`:

```python
# Topnav entries whose rail answer is a REDIRECT into the Library rather
# than a link: the page still exists (topnav renders it), but under rail it
# folds into the Library section the redirect names. Kept separate from
# KNOWN_TOPNAV_ONLY because these are not "deliberately unreachable" — a
# behavioral test below proves each really redirects, so this set can never
# rot into a silent allowlist.
REDIRECTED_UNDER_RAIL = {
    "/corporate-memory": "/library?section=memory_domain",
    "/apps": "/library?section=data_app",
}
```

Change the parity assertion to subtract `set(REDIRECTED_UNDER_RAIL)` as well:

```python
def test_every_topnav_user_page_is_reachable_under_rail():
    missing = (
        _topnav_user_links()
        - _rail_reachable_links()
        - KNOWN_TOPNAV_ONLY
        - set(REDIRECTED_UNDER_RAIL)
    )
    assert not missing, (...)  # keep the existing message
```

And add the behavioral proof (needs the `seeded_app` fixture — add the import-free fixture usage like the sibling nav tests):

```python
def test_redirected_entries_really_redirect(seeded_app, monkeypatch):
    """REDIRECTED_UNDER_RAIL is a claim, not an allowlist: every entry must
    actually 302 into its Library section under rail."""
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
    monkeypatch.setenv("AGNES_DATA_APPS_ENABLED", "1")
    c = seeded_app["client"]
    headers = {"Authorization": f"Bearer {seeded_app['analyst_token']}"}
    for src, target in REDIRECTED_UNDER_RAIL.items():
        resp = c.get(src, headers=headers, follow_redirects=False)
        assert resp.status_code == 302, f"{src} did not redirect under rail"
        assert resp.headers["location"] == target
```

In `tests/test_web_memory_domain_detail.py`, revert the rail back-link test to the Library target (rename back, keep the hero-scoped assertions):

```python
    def test_back_link_targets_the_library_under_rail(self, seeded_app, monkeypatch):
        # Under rail /corporate-memory REDIRECTS to the Library's Memory band
        # (spec 2026-08-12), so back links point straight at the band rather
        # than bouncing through the redirect. Scoped to the hero's own link.
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        dom_id = _make_domain("ops-rail", "Ops Rail")
        _make_item("ops_rail_item_1", "Ops rail runbook", dom_id)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/memory/d/ops-rail", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.text
        assert 'class="detail-back" href="/library?section=memory_domain"' in body
        assert "/catalog?kind=memory" not in body
        assert 'class="detail-back" href="/corporate-memory"' not in body
```

- [ ] **Step 2: Run to verify current state fails**

Run: `.venv/bin/pytest tests/test_web_nav_user_parity.py tests/test_web_memory_domain_detail.py -q --tb=short`
Expected: back-link test FAILS (router still points at /corporate-memory); parity tests pass (rows still present is fine — the subtraction only widens the allowance).

- [ ] **Step 3: Implement — rows out, back target restored**

In `app/web/router.py` restore the Library target (replace the #1276 entry and its comment):

```python
    # Memory folds into the Library under rail (spec 2026-08-12): the
    # standalone /corporate-memory page 302s to this section, so back links
    # point straight at the band rather than bouncing through the redirect.
    "memory_domain": ("/library?section=memory_domain", "All memory"),
```

In `app/web/templates/_app_rail.html`:
- Delete the whole Memory `<a class="rail-i ...` block (the one linking `/corporate-memory`) including its leading comment.
- Delete the whole Apps `{% if data_apps_enabled() %}...{% endif %}` block (the one linking `/apps`) including its leading comment.
- In the header IA comment, change "the browse destinations Library · Agents · Memory · Apps" back to "the browse destinations Library · Agents", and append one sentence: "Memory and Data apps fold into the Library (their standalone pages 302 there — see tests/test_web_nav_user_parity.py::REDIRECTED_UNDER_RAIL)."

- [ ] **Step 4: Run the affected suites**

Run: `.venv/bin/pytest tests/test_web_nav_user_parity.py tests/test_web_memory_domain_detail.py tests/test_web_nav_me_connections.py tests/test_admin_nav_parity.py tests/test_web_rail_redirects.py -q --tb=short`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/web/templates/_app_rail.html app/web/router.py tests/test_web_nav_user_parity.py tests/test_web_memory_domain_detail.py
git commit -m "feat(ui): rail nav slims back to Library · Agents · Admin — Memory and Apps live in the Library"
```

---

### Task 5: CHANGELOG, full verification, push, draft PR

**Files:**
- Modify: `CHANGELOG.md` (`[Unreleased]` → Changed)
- Modify: `docs/superpowers/plans/2026-08-12-library-memory-dataapps-merge.md` (check off tasks)

- [ ] **Step 1: CHANGELOG bullet**

Under `## [Unreleased]` add a `### Changed` section (or extend it):

```markdown
### Changed

- **Under the rail chrome, the Library is now the one browse surface for memory domains and hosted data apps.** The Memory band carries the item/required counts the standalone cards had and hides empty optional domains; a new Data apps band lists the caller's visible apps (gated on `data_apps.enabled`), fulfilling the Files band's "Data apps coming soon" promise. `/corporate-memory` and `/apps` now 302 to their Library sections under rail — the interim rail nav rows for both are gone again. Default (topnav) instances render exactly what they rendered before; detail pages (`/memory/d/…`, `/apps/detail/…`) stay live in both chromes.
```

- [ ] **Step 2: Verification sweep (verify-agnes-change order)**

```bash
.venv/bin/python scripts/verify_syncmap.py
.venv/bin/pytest tests/test_web_library_data_apps.py tests/test_web_library_memory_band.py tests/test_web_rail_redirects.py tests/test_web_nav_user_parity.py tests/test_web_memory_domain_detail.py tests/test_ui_layout_theme.py tests/test_design_system_contract.py tests/test_web_library_sharing.py tests/test_web_library_store_entities.py tests/test_corporate_memory_page.py tests/test_data_apps_api.py -q --tb=short
```

Expected: syncmap clean; all listed suites PASS (notably `TestDefaultContentParity` inside test_ui_layout_theme).

- [ ] **Step 3: Commit + push + draft PR**

```bash
git add CHANGELOG.md docs/superpowers/plans/2026-08-12-library-memory-dataapps-merge.md
git commit -m "docs: changelog for the Library memory/data-apps merge"
git push -u origin HEAD:refs/heads/zs/library-memory-dataapps-merge
gh pr create --draft --base zs/connections-new-ui --head zs/library-memory-dataapps-merge --title "Library absorbs the Memory browse; Data apps become a Library band (rail-only)" --body "Implements docs/superpowers/specs/2026-08-12-library-memory-dataapps-merge-design.md. Stacked on #1276. NOTE: stacked PRs get no automatic CI — dispatch ci.yml manually or retarget to main after #1276 merges."
```

Note: a stacked PR (base ≠ main) triggers no CI — dispatch the workflow manually (`gh workflow run ci.yml --ref zs/library-memory-dataapps-merge`) or wait until #1276 merges and retarget the base to `main`.
