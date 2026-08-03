"""Route tests for the generic authoring-agent studio pages."""

import pytest

DOMAINS = ["data-package", "mcp", "marketplace", "corporate-memory"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize("domain", DOMAINS)
def test_studio_renders_for_admin_in_create_mode(seeded_app, domain):
    c = seeded_app["client"]
    resp = c.get(f"/admin/studio/{domain}", headers=_auth(seeded_app["admin_token"]))
    assert resp.status_code == 200
    body = resp.text
    assert 'id="studio-create"' in body
    assert "/static/js/studio.js" in body
    assert "window.STUDIO" in body
    assert "isAdmin: true" in body
    assert ">Create<" in body  # admin sees the direct-create action


@pytest.mark.parametrize("domain", DOMAINS)
def test_studio_renders_for_non_admin_in_submit_mode(seeded_app, domain):
    c = seeded_app["client"]
    resp = c.get(f"/admin/studio/{domain}", headers=_auth(seeded_app["analyst_token"]))
    assert resp.status_code == 200
    body = resp.text
    assert "isAdmin: false" in body
    assert "Submit for approval" in body  # non-admin sees the suggestion action


def test_studio_index_title_carries_instance_name(seeded_app):
    """Regression: /admin/studio rendered ``<title>Studio — </title>`` — the
    title template reads ``config.INSTANCE_NAME`` but ``_chrome_ctx`` didn't
    provide ``config``, so Jinja rendered the undefined as empty. The title
    must carry the instance name (default: "AI Harness")."""
    import re

    c = seeded_app["client"]
    resp = c.get("/admin/studio", headers=_auth(seeded_app["admin_token"]))
    assert resp.status_code == 200
    m = re.search(r"<title>(.*?)</title>", resp.text, re.S)
    assert m is not None
    title = m.group(1).strip()
    assert title.startswith("Studio — ")
    assert len(title) > len("Studio — "), f"empty instance name in title: {title!r}"


def test_studio_requires_login(seeded_app):
    c = seeded_app["client"]
    # No auth header → redirect to login (don't follow it) or 401/403.
    resp = c.get("/admin/studio/data-package", follow_redirects=False)
    assert resp.status_code in (302, 307, 401, 403)
    if resp.status_code in (302, 307):
        assert "/login" in resp.headers.get("location", "")


def test_studio_unknown_domain_404s(seeded_app):
    c = seeded_app["client"]
    resp = c.get("/admin/studio/nope", headers=_auth(seeded_app["admin_token"]))
    assert resp.status_code == 404


def test_suggestions_review_page_renders_for_admin(seeded_app):
    c = seeded_app["client"]
    resp = c.get("/admin/studio/suggestions", headers=_auth(seeded_app["admin_token"]))
    assert resp.status_code == 200
    assert "/static/js/studio_suggestions.js" in resp.text
    assert 'id="sug-list"' in resp.text
    assert 'id="sug-run-mining"' in resp.text  # admin can trigger a mining run


def test_memory_mining_consent_page_renders_for_user(seeded_app):
    c = seeded_app["client"]
    resp = c.get("/me/memory-mining", headers=_auth(seeded_app["analyst_token"]))
    assert resp.status_code == 200
    assert 'id="mm-toggle"' in resp.text
    assert "/static/js/me_memory_mining.js" in resp.text


def test_suggestions_review_page_requires_admin(seeded_app):
    c = seeded_app["client"]
    resp = c.get(
        "/admin/studio/suggestions",
        headers=_auth(seeded_app["analyst_token"]),
        follow_redirects=False,
    )
    assert resp.status_code in (302, 307, 401, 403)


def test_skill_domain_registered_as_direct_submit():
    from app.web.studio import STUDIO_DOMAINS, get_domain

    spec = get_domain("skill")
    assert spec is not None
    assert spec.submit_directly is True
    assert spec.endpoint == "/api/store/entities/from-markdown"
    assert spec.profile == "skill-author"
    assert [f.key for f in spec.fields] == ["name", "description", "category", "skill_md"]
    # every other domain except "agent" (the store's other direct-submit
    # type) still routes through the suggestions queue
    assert all(not d.submit_directly for s, d in STUDIO_DOMAINS.items() if s not in ("skill", "agent"))


def test_agent_domain_registered_as_direct_submit():
    from app.web.studio import get_domain

    spec = get_domain("agent")
    assert spec is not None
    assert spec.submit_directly is True
    assert spec.endpoint == "/api/store/entities/from-markdown"
    assert spec.profile == "agent-author"
    assert [f.key for f in spec.fields] == ["name", "description", "category", "skill_md"]


def test_agent_studio_renders_publish_for_non_admin(seeded_app):
    c = seeded_app["client"]
    resp = c.get("/admin/studio/agent", headers=_auth(seeded_app["analyst_token"]))
    assert resp.status_code == 200
    body = resp.text
    assert "isAdmin: false" in body
    assert "submitDirect: true" in body
    assert ">Publish<" in body
    assert "Submit for approval" not in body


def test_agent_studio_renders_for_admin(seeded_app):
    c = seeded_app["client"]
    resp = c.get("/admin/studio/agent", headers=_auth(seeded_app["admin_token"]))
    assert resp.status_code == 200
    assert "submitDirect: true" in resp.text
    assert 'id="studio-f-skill_md"' in resp.text  # the agent content textarea rendered
    assert 'domain: "agent"' in resp.text


def test_skill_studio_renders_publish_for_non_admin(seeded_app):
    c = seeded_app["client"]
    resp = c.get("/admin/studio/skill", headers=_auth(seeded_app["analyst_token"]))
    assert resp.status_code == 200
    body = resp.text
    assert "isAdmin: false" in body
    assert "submitDirect: true" in body
    assert ">Publish<" in body  # direct-submit domains publish, not suggest
    assert "Submit for approval" not in body
    assert "store" in body.lower()  # footer explains the store review pipeline


def test_skill_studio_renders_for_admin(seeded_app):
    c = seeded_app["client"]
    resp = c.get("/admin/studio/skill", headers=_auth(seeded_app["admin_token"]))
    assert resp.status_code == 200
    assert "submitDirect: true" in resp.text
    assert 'id="studio-f-skill_md"' in resp.text  # the markdown textarea rendered


def test_skills_page_is_the_unified_builder(seeded_app):
    """/skills IS the builder now, and it builds all three authored kinds.

    The separate "your skills" index was retired first (created items land in
    the Library); the single-TYPE builder was retired next. The page opens on
    a type picker, then swaps content per type inside one shell — access
    picker, numbered sections, one primary action."""
    c = seeded_app["client"]
    resp = c.get("/skills", headers=_auth(seeded_app["analyst_token"]))
    assert resp.status_code == 200
    body = resp.text
    # Retired: the index container and its "+ New skill" grid card.
    assert 'id="sk-list-view"' not in body
    assert "renderList" not in body
    # The builder is the whole page.
    assert 'id="sk-builder-view"' in body
    assert 'id="sk-categories"' in body  # store-category options island
    # Step 1 — every supported type is offered, and picking one is what opens
    # the form (no type ⇒ no builder).
    # The cards are built client-side from the TYPES table, so assert on the
    # table + the hook each card carries, not on markup the server never emits.
    assert "What are you building?" in body
    assert "data-sk-type" in body
    assert "var TYPE_ORDER = ['skill', 'plugin', 'agent'];" in body
    for kind in ("skill", "plugin", "agent"):
        assert f"key: '{kind}'," in body
    # Type is a COLLAPSING step inside the form, not a separate picker screen:
    # one numbering sequence (1 Type → 2 Identity → 3 content), and answering
    # it collapses to a summary + Change rather than navigating away.
    assert "data-sk-change" in body
    assert ">Change<" in body
    assert "sk-sec--done" in body
    for n in ("1", "2", "3"):
        assert f'class="sk-sec-no">{n}<' in body or f'"sk-sec-no">{n}<' in body
    # Access is a required choice before saving: Private or the whole org.
    assert 'name="sk-access"' in body
    assert 'value="private"' in body
    assert 'value="everyone"' in body
    assert "Who can use this" in body
    # One primary action. A draft is explicitly local, never a store write —
    # "Publish to marketplace" stays gone.
    assert 'id="sk-save"' in body
    assert "Save to Library" in body
    assert "Publish to marketplace" not in body
    assert 'id="sk-draft"' in body
    # Saving returns to the Library and highlights the new item.
    assert "/library?new=" in body
    # Both publish paths are wired: markdown for skills/agents, multipart for
    # plugin bundles, and the bundle is validated before it can be saved.
    assert "/api/store/entities/from-markdown" in body
    assert "'/api/store/entities'" in body
    assert "/api/store/entities/preview" in body
    # Regression: the builder's buttons use the shared .cc-btn styles.
    assert "css/catalog_card.css" in body


def test_markdown_body_can_be_written_or_uploaded(seeded_app):
    """A skill / shareable agent body can be UPLOADED as a .md, offered as an
    explicit choice next to writing it.

    The file route existed before this, as a 12.5px dashed strip above a 14-row
    textarea — read as decoration, so authors who already had a SKILL.md pasted
    it in by hand. Worse, its "browse" affordance was a <label for> pointing at
    a `hidden` input: a label is not keyboard-focusable and neither is a hidden
    input, so the file route could not be reached without a mouse at all."""
    body = seeded_app["client"].get("/skills", headers=_auth(seeded_app["analyst_token"])).text

    # Both routes, at the same weight, as a pressed-state toggle group.
    assert "data-sk-mode" in body
    assert "'Write it here'" in body
    assert "'Upload a .md file'" in body
    assert 'aria-pressed="' in body
    # Reachable by keyboard: a real button opens the hidden input via .click().
    assert "data-sk-browse" in body
    assert ">Choose a file<" in body
    assert 'for="sk-file-input"' not in body  # the label affordance it replaced
    assert "input.click()" in body
    # Upload is an INPUT METHOD, not a mode: an uploaded file drops into the
    # same body the textarea edits, and there is a way back to the editor.
    assert 'data-sk-mode="write">Edit as text<' in body
    # An uploaded .md carries its own identity — reading it beats making the
    # author retype what they just handed over. Blanks only, never a clobber.
    assert "function parseFrontmatter(" in body
    assert "function applyFrontmatter(" in body
    assert "filled in " in body  # the receipt naming which fields were filled
    # Guards the intake actually needs: text-only, capped, and never a silent
    # overwrite of work already written.
    assert "MAX_MD_BYTES" in body
    assert "is not a Markdown file" in body
    assert "window.confirm('Replace the '" in body
    # The plugin bundle is a .zip and cannot be typed, so it gets no toggle —
    # but it shares the file card, and the same keyboard-reachable browse.
    assert "'.zip,application/zip'" in body


def test_builder_separates_shareable_agents_from_personal_ones(seeded_app):
    """A shareable agent (a Library resource anyone can install) and a personal
    agent (configured on /agents, carrying its owner's scopes) are one word
    apart. The builder must say which one it is making, and point at the other
    — getting this wrong costs an author a whole draft."""
    c = seeded_app["client"]
    body = c.get("/skills", headers=_auth(seeded_app["analyst_token"])).text
    assert "shareable agent" in body
    assert 'href="/agents"' in body
    assert "personal agents" in body
    # It must not claim the author's own authority travels with the resource.
    assert "Nothing you write here inherits your access." in body


def test_skills_page_arms_new_skill_spotlight(seeded_app):
    """/skills still carries the one-step coach-mark that the Marketplace's
    "Submit a skill or plugin" CTA arrives with (`?spotlight=new-skill`)."""
    c = seeded_app["client"]
    body = c.get("/skills", headers=_auth(seeded_app["analyst_token"])).text

    assert "spotlight" in body and "new-skill" in body
    assert "js/tour.js" in body  # lazy dynamic import of the engine
    assert "launchTour('skill-builder')" in body
    # The guard must accept the type step in EITHER state. It briefly required
    # the name field, which is only in the DOM once a type is chosen — on a
    # cold arrival (step 1 expanded) the coach-mark had no anchor at all.
    assert "'[data-sk-type],[data-sk-change]'" in body
    # One-shot: the param is stripped so a reload doesn't re-pop the coach-mark.
    assert "history.replaceState" in body
    assert "maybeSpotlightNew()" in body
    # The CTA promises "skill or plugin". Plugins are built right here now, so
    # the onward path is the Plugin type itself — the builder must NOT hand the
    # author off to the old curated-marketplace guide, a concept the new UI no
    # longer carries.
    assert "/marketplace/guide/curated" not in body


def test_skill_builder_tour_anchors_on_the_type_step():
    """The `skill-builder` tour is a single step on /skills, anchored on the
    TYPE step — the first thing on the page and the first decision to make.

    Anchor history is the point of this guard: it was the "+ New skill" card,
    then the name field, and the name field broke when type became step 1
    (that field is not in the DOM until a type is picked, so the coach-mark
    pointed at nothing). The type step is present in both its states, so the
    selector must cover the expanded cards AND the collapsed Change button.
    Single-step tours render in the popover's solo form (no dots / no "explore
    on my own"), so guard the branch that produces it too."""
    from pathlib import Path

    js = Path("app/web/static/js/tour.js").read_text()
    assert "'skill-builder':" in js
    assert "'[data-sk-type], [data-sk-change]'" in js
    assert "[data-sk-new]" not in js  # retired anchor
    assert '[data-sk-field="name"]' not in js  # retired anchor — breaks on cold arrival
    assert "page: '/skills'" in js
    # Solo rendering: one step ⇒ 'Got it', no dots, no escape-hatch button.
    assert "const solo = total === 1;" in js
    assert "tour-popover-footer--solo" in js
    assert Path("app/web/static/css/tour.css").read_text().count(".tour-popover-footer--solo"), (
        "solo footer modifier must be styled or the action row sits left"
    )


def test_skills_index_requires_login(seeded_app):
    c = seeded_app["client"]
    resp = c.get("/skills", follow_redirects=False)
    assert resp.status_code in (302, 307, 401, 403)
    if resp.status_code in (302, 307):
        assert "/login" in resp.headers.get("location", "")


def test_existing_domains_keep_suggestion_flow(seeded_app):
    c = seeded_app["client"]
    resp = c.get("/admin/studio/data-package", headers=_auth(seeded_app["analyst_token"]))
    assert "submitDirect: false" in resp.text
    assert "Submit for approval" in resp.text


def test_studio_index_lists_every_domain(seeded_app):
    from app.web.studio import STUDIO_DOMAINS

    c = seeded_app["client"]
    resp = c.get("/admin/studio", headers=_auth(seeded_app["analyst_token"]))
    assert resp.status_code == 200
    body = resp.text
    for slug, domain in STUDIO_DOMAINS.items():
        assert f"/admin/studio/{slug}" in body
        assert domain.title in body


def test_studio_index_requires_login(seeded_app):
    c = seeded_app["client"]
    resp = c.get("/admin/studio", follow_redirects=False)
    assert resp.status_code in (302, 307, 401, 403)
    if resp.status_code in (302, 307):
        assert "/login" in resp.headers.get("location", "")


def test_primary_nav_links_to_studio(seeded_app):
    c = seeded_app["client"]
    resp = c.get("/admin/studio", headers=_auth(seeded_app["analyst_token"]))
    assert resp.status_code == 200
    assert 'href="/admin/studio"' in resp.text


# --- Instance-level enable/disable toggle (studio.enabled / AGNES_STUDIO_ENABLED) ---


def test_studio_routes_redirect_when_disabled(seeded_app, monkeypatch):
    # get_studio_enabled is imported into the router namespace and consulted by
    # every studio handler + both chrome builders — patch it there.
    monkeypatch.setattr("app.web.router.get_studio_enabled", lambda: False)
    c = seeded_app["client"]
    for path in ("/admin/studio", "/admin/studio/data-package", "/admin/studio/suggestions"):
        resp = c.get(path, headers=_auth(seeded_app["admin_token"]), follow_redirects=False)
        assert resp.status_code in (302, 307), path
        assert resp.headers.get("location", "") == "/", path


def test_studio_nav_hidden_when_disabled(seeded_app, monkeypatch):
    c = seeded_app["client"]
    # Sanity: link + palette entries present by default on BOTH chrome paths —
    # /me/memory-mining renders via _chrome_ctx, /dashboard via _build_context.
    for page in ("/me/memory-mining", "/dashboard"):
        resp = c.get(page, headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200, page
        assert 'data-tour="nav-studio"' in resp.text, page
        assert "Studio · Data package" in resp.text, page  # command palette
    # Disable → nav entry AND palette items disappear on both paths (the route
    # stays reachable only by URL, which the redirect test covers).
    monkeypatch.setattr("app.web.router.get_studio_enabled", lambda: False)
    for page in ("/me/memory-mining", "/dashboard"):
        resp = c.get(page, headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200, page
        assert 'data-tour="nav-studio"' not in resp.text, page
        assert "Studio · Data package" not in resp.text, page


def test_studio_enabled_env_override(monkeypatch):
    import app.instance_config as ic

    ic.reset_cache()
    # Every documented false-like env spelling disables.
    for falsy in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("AGNES_STUDIO_ENABLED", falsy)
        assert ic.get_studio_enabled() is False, falsy
    monkeypatch.setenv("AGNES_STUDIO_ENABLED", "true")
    assert ic.get_studio_enabled() is True
    monkeypatch.delenv("AGNES_STUDIO_ENABLED", raising=False)
    # No env, no yaml studio block → defaults on.
    assert ic.get_studio_enabled() is True


def test_studio_enabled_yaml_fallback_and_precedence(monkeypatch):
    """studio.enabled: false in YAML disables; env still wins over YAML."""
    import app.instance_config as ic

    def fake_get_value(*keys, default=None):
        if keys == ("studio", "enabled"):
            return False
        return default

    monkeypatch.setattr(ic, "get_value", fake_get_value)
    monkeypatch.delenv("AGNES_STUDIO_ENABLED", raising=False)
    assert ic.get_studio_enabled() is False  # YAML fallback
    monkeypatch.setenv("AGNES_STUDIO_ENABLED", "1")
    assert ic.get_studio_enabled() is True  # env > YAML
