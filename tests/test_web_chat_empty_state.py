"""Web UI route — the ``/chat`` pre-conversation Dashboard (issue #896).

The rail empty state is the Dashboard: greeting, the real composer, a
"Agnes is using N knowledge sources and M capabilities from your Stack"
context line, activity panels, and guided task starters. (Its ancestors —
the standalone ``/ask`` hero, then the ``/chat`` "Ask anything." hero with
the "Operated by Agnes" pill — are retired.) The counts are the caller's
ACTUAL Stack contents, matching the /stack page the line links to:
knowledge sources = ``StackResolver.stack()`` over data packages + memory
domains (``_stack_knowledge_source_count``); capabilities = the
``?tab=my`` plugin roster — subscribed/required curated plugins ∩ RBAC,
plus Store installs (``_stack_capability_count``). NOT everything the
caller could browse or add. These tests render ``/chat``'s rail empty
state and assert that counting + pluralization.

Rendering it needs three things: rail layout, an enabled chat backend,
and CHAT *access* (admin clears it via god-mode; a normal user needs a
``chat`` grant to pass the route's default-deny guard).
"""

from __future__ import annotations

from types import SimpleNamespace


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _make_pkg(slug: str, name: str) -> str:
    from src.db import get_system_db
    from src.repositories.data_packages import DataPackagesRepository

    conn = get_system_db()
    try:
        return DataPackagesRepository(conn).create(
            name=name,
            slug=slug,
            description=f"{name} desc",
            icon="\U0001f4e6",
            color="#fce7f3",
            created_by="test",
        )
    finally:
        conn.close()


def _grant(
    group_name: str,
    resource_type: str,
    resource_id: str,
    requirement: str = "available",
    users: list[str] | None = None,
) -> None:
    """Add a resource_grants row for the named user-group.

    Mirrors the helper in ``tests/test_web_catalog_unified.py`` — also
    ensures ``users`` are members of the group (seeded_app only puts
    admin1 in the Admin group; everyone else starts with zero memberships).
    """
    import uuid
    from src.db import get_system_db
    from src.repositories.user_group_members import UserGroupMembersRepository

    conn = get_system_db()
    try:
        gid = conn.execute("SELECT id FROM user_groups WHERE name = ?", [group_name]).fetchone()
        if not gid:
            return
        group_id = gid[0]
        if users:
            members = UserGroupMembersRepository(conn)
            for u in users:
                try:
                    members.add_member(u, group_id, source="test")
                except Exception:
                    pass
        conn.execute(
            "INSERT INTO resource_grants(id, group_id, resource_type, resource_id, "
            "requirement, assigned_at, assigned_by) "
            "VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, 'test')",
            [str(uuid.uuid4()), group_id, resource_type, resource_id, requirement],
        )
    finally:
        conn.close()


def _enable_rail_chat(seeded_app, monkeypatch) -> None:
    """Make ``/chat`` render its rail empty-state hero: rail chrome + an
    enabled chat backend. Callers still need CHAT *access* — admin via
    god-mode, or a ``_grant(..., "chat", "chat", ...)`` for a normal user."""
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
    seeded_app["client"].app.state.chat_config = SimpleNamespace(enabled=True)


class TestChatEmptyStatePill:
    def test_renders_dashboard_and_context_line(self, seeded_app, monkeypatch):
        """Rail ``/chat`` empty state renders the Dashboard — the Knowledge
        Layer lead as the page hero, activity panels, guided task starters —
        and the Stack context line (a package granted to the admin's group
        puts it in their Stack, so the knowledge-source count is non-zero)."""
        _enable_rail_chat(seeded_app, monkeypatch)
        pkg_id = _make_pkg("dash-ctx-pkg", "Dash ctx pkg")
        _grant("Admin", "data_package", pkg_id)
        c = seeded_app["client"]
        resp = c.get("/chat", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200, resp.text
        body = resp.text
        # The Knowledge Layer hero — a single premium banner: text lead + CTA
        # on the left, orb in the centre, floating integration chips on the
        # right (no inner "Ask Agnes" / tools cards).
        assert '<div class="klb-lead">' in body
        assert "Agnes is your knowledge layer." in body
        assert "Use it here or connect your tools." in body
        assert "Connect your tools" in body  # the green CTA
        # Floating integration chips (no surrounding card).
        assert 'class="klb-chips"' in body
        assert "Claude Code" in body and "CLI and more" in body
        # Below the banner: the trust caption + the "Ask Agnes anything" heading.
        assert "Secure. Private. Always in sync." in body
        assert "Ask Agnes anything" in body
        assert 'id="rdb-actions"' in body
        assert "Agnes is using" in body and "from your Stack" in body
        # The retired hero copy must be gone.
        assert "Ask anything." not in body
        assert "Operated by" not in body
        assert "Suggested questions" not in body

    def test_context_line_links_to_library_in_stack_filter(self, seeded_app, monkeypatch):
        """Both counts link to the Library with "In stack only" pre-applied, NOT
        to /stack: My Stack is not a rail destination any more (#1088), so the
        old href landed the caller on a page with no nav entry. /library renders
        every kind that page did and the filter narrows it to what the line
        counts."""
        _enable_rail_chat(seeded_app, monkeypatch)
        pkg_id = _make_pkg("ctx-link-pkg", "Ctx link pkg")
        _grant("Admin", "data_package", pkg_id)
        resp = seeded_app["client"].get("/chat", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200, resp.text
        body = resp.text
        assert "Agnes is using" in body
        # The status line's own markup — scoped so a stray /stack link elsewhere
        # in the page can't pass this off as fixed.
        line = body[body.index('<p class="rdb-context">') :]
        line = line[: line.index("</p>")]
        assert line.count('href="/library?stack=in_stack"') == 2
        assert 'href="/stack"' not in line

    def test_dashboard_carries_the_first_run_orientation_line(self, seeded_app, monkeypatch):
        """ "New here? See how Agnes works" — the PROMINENT path to
        /how-it-works, which the rail carries only as a quiet `.rail-meta` row in
        its foot (_app_rail.html). A newcomer needs the explainer far more than a
        returning user does, and the empty state is where their eyes already are.

        Below the composer, not above it: directly over the input is the moment
        of INTENT, and a navigate-away control was retired from there once
        already (#1108's "Browse metrics & glossary"). Inside
        `#chat-empty-extras`, so it retires with the rest of the dashboard as
        soon as a conversation starts.
        """
        _enable_rail_chat(seeded_app, monkeypatch)
        resp = seeded_app["client"].get("/chat", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200, resp.text
        body = resp.text
        extras = body[body.index('id="chat-empty-extras"') :]
        extras = extras[: extras.index('id="chat-empty-banner"')]
        assert 'class="rdb-orient"' in extras
        assert "New here?" in extras
        assert 'href="/how-it-works"' in extras
        assert "See how Agnes works" in extras  # brand-templated, seeded default
        # Below the Stack status line it shares a register with — the two read as
        # a pair of quiet notes under the input, in that order.
        assert extras.index('class="rdb-context"') < extras.index('class="rdb-orient"')
        # And it is NOT a third CTA in the hero, which keeps "Connect your tools"
        # as its single action (see tests/test_ui_layout_theme.py).
        assert "rdb-orient" not in body[: body.index('id="chat-empty-extras"')]

    def test_context_line_hidden_at_zero(self, seeded_app, monkeypatch):
        """analyst1 has no data/plugin grants → both counts are 0 and the
        context line hides entirely ("Agnes is using 0 knowledge sources"
        would read as broken). The CHAT grant only unlocks the route; it is
        not a knowledge source, so it doesn't bump N."""
        _enable_rail_chat(seeded_app, monkeypatch)
        _grant("Everyone", "chat", "chat", users=["analyst1"])
        c = seeded_app["client"]
        resp = c.get("/chat", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200, resp.text
        assert "Agnes is using" not in resp.text
        # The rest of the dashboard still renders.
        assert 'id="rdb-actions"' in resp.text

    def test_context_line_reflects_rbac_grant(self, seeded_app, monkeypatch):
        """A required data-package grant on the analyst's group bumps N —
        the line appears, pluralized down to the singular "source" at N=1."""
        _enable_rail_chat(seeded_app, monkeypatch)
        _grant("Everyone", "chat", "chat", users=["analyst1"])
        pkg_id = _make_pkg("ask-landing-pkg", "Ask landing pkg")
        _grant("Everyone", "data_package", pkg_id, requirement="required", users=["analyst1"])
        c = seeded_app["client"]
        resp = c.get("/chat", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200, resp.text
        assert "Agnes is using" in resp.text
        assert ">1 knowledge source</a>" in resp.text
        # Singular, not plural — the plural fragment must be absent.
        assert "1 knowledge sources</a>" not in resp.text

    def test_admin_counts_stack_not_catalog(self, seeded_app, monkeypatch):
        """The line reads the caller's ACTUAL Stack, not the whole catalog:
        creating a package does NOT bump the admin's count (god-mode lets
        admin browse everything, but browse ≠ Stack); an admin self-serve
        subscribe (POST /api/stack/subscribe, no grant needed) does."""
        import re

        _enable_rail_chat(seeded_app, monkeypatch)
        c = seeded_app["client"]
        headers = _auth(seeded_app["admin_token"])

        def _count() -> int:
            resp = c.get("/chat", headers=headers)
            assert resp.status_code == 200, resp.text
            m = re.search(r">(\d+) knowledge source", resp.text)
            return int(m.group(1)) if m else 0

        before_n = _count()
        pkg_id = _make_pkg("stack-count-pkg", "Stack count pkg")
        assert _count() == before_n  # catalog growth alone isn't the Stack

        resp = c.post(
            "/api/stack/subscribe",
            headers=headers,
            json={"resource_type": "data_package", "resource_id": pkg_id},
        )
        assert resp.status_code == 200, resp.text
        assert _count() == before_n + 1

    def test_capabilities_count_pluralization(self, seeded_app, monkeypatch):
        """capability_count == 1 renders the singular "1 capability from your
        Stack" — and only SUBSCRIBED plugins count: RBAC resolves two
        plugins for the caller, one subscription row exists, so M == 1.
        Assert the exact pill fragment — the empty-state DOM also carries
        an ``id="chat-capabilities"``, so a bare "capabilities" substring
        check would be a false negative."""
        from src import marketplace_filter
        from src.repositories import user_curated_subscriptions_repo

        _enable_rail_chat(seeded_app, monkeypatch)
        monkeypatch.setattr(
            marketplace_filter,
            "resolve_allowed_plugins",
            lambda conn, user: [
                {"marketplace_id": "mp1", "original_name": "demo-plugin", "manifest_name": "demo-plugin", "raw": {}},
                {"marketplace_id": "mp1", "original_name": "other-plugin", "manifest_name": "other-plugin", "raw": {}},
            ],
        )
        user_curated_subscriptions_repo().subscribe("admin1", "mp1", "demo-plugin")
        c = seeded_app["client"]
        resp = c.get("/chat", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200, resp.text
        assert ">1 capability</a>" in resp.text
        assert ">1 capabilities</a>" not in resp.text

    def test_requires_login(self, seeded_app):
        """Same auth gate as every other authenticated page — unauthenticated
        requests redirect to /login rather than rendering (TestClient
        follows redirects by default, so assert on the pre-redirect hop)."""
        c = seeded_app["client"]
        resp = c.get("/chat", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"].startswith("/login")

    def test_rail_empty_state_has_no_semantic_layer_detour(self, seeded_app, monkeypatch):
        """The empty state offers NO browse link to /catalog/semantics.

        It carried one (#1108) directly above the composer. Retired: the empty
        state is the moment of intent — the reader came to ask something — so a
        control whose only function is to navigate away from the composer,
        offered before they have an answer to check, is a detour. The semantic
        layer is reached from the rail's Definitions nav row, the Library's
        Definitions band, and search.

        Asserted on the button's own wrapper class rather than the bare
        `/catalog/semantics` URL: the rail chrome carries a Definitions nav row
        on every page, this one included, so the URL is legitimately in the
        markup. What must not come back is the in-page control.
        """
        _enable_rail_chat(seeded_app, monkeypatch)
        c = seeded_app["client"]
        resp = c.get("/chat", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        assert "rdb-semantic-links" not in resp.text
        assert "Browse metrics" not in resp.text
