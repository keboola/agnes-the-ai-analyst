"""Web UI route — the ``/chat`` pre-conversation Dashboard (issue #896).

The rail empty state is the Dashboard: greeting, the real composer, a
"Kai is using N knowledge sources and M capabilities from your Stack"
context line, activity panels, and guided task starters. (Its ancestors —
the standalone ``/ask`` hero, then the ``/chat`` "Ask anything." hero with
the "Operated by Kai" pill — are retired.) The counts are the caller's
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
        """Rail ``/chat`` empty state renders the Dashboard — greeting,
        activity panels, guided task starters — and the Stack context
        line (a package granted to the admin's group puts it in their
        Stack, so the knowledge-source count is non-zero)."""
        _enable_rail_chat(seeded_app, monkeypatch)
        pkg_id = _make_pkg("dash-ctx-pkg", "Dash ctx pkg")
        _grant("Admin", "data_package", pkg_id)
        c = seeded_app["client"]
        resp = c.get("/chat", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200, resp.text
        body = resp.text
        assert 'id="rdb-greeting-tod"' in body
        # The Knowledge Layer banner — the product-model hero (compact mode
        # drops the headline lead; the aria-label + cards remain).
        assert "One knowledge layer. Everywhere you work." in body
        assert "Ask Kai in Agnes" in body
        assert "Use your own AI tools" in body
        assert "Connect your tools" in body
        assert 'id="rdb-actions"' in body
        assert "Kai is using" in body and "from your Stack" in body
        # The retired hero copy must be gone.
        assert "Ask anything." not in body
        assert "Operated by" not in body
        assert "Suggested questions" not in body

    def test_context_line_hidden_at_zero(self, seeded_app, monkeypatch):
        """analyst1 has no data/plugin grants → both counts are 0 and the
        context line hides entirely ("Kai is using 0 knowledge sources"
        would read as broken). The CHAT grant only unlocks the route; it is
        not a knowledge source, so it doesn't bump N."""
        _enable_rail_chat(seeded_app, monkeypatch)
        _grant("Everyone", "chat", "chat", users=["analyst1"])
        c = seeded_app["client"]
        resp = c.get("/chat", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200, resp.text
        assert "Kai is using" not in resp.text
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
        assert "Kai is using" in resp.text
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
