"""Web UI route — GET /ask, the knowledge-search chat landing (issue #896
prototype "Ask anything / Reuse everything")."""

from __future__ import annotations


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


def _grant(group_name: str, resource_type: str, resource_id: str,
           requirement: str = "available", users: list[str] | None = None) -> None:
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


class TestAskLanding:
    def test_renders_prototype_copy(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/ask", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        body = resp.text
        assert "Ask anything." in body
        assert "Reuse" in body
        assert "everything." in body
        assert "Your company" in body
        assert "skills" in body and "colleagues built" in body
        assert "every AI" in body
        assert "How does customer onboarding work?" in body
        assert "Suggested questions" in body
        assert "Summarize the pricing deck" in body
        assert "Tell me about our customer segments" in body
        assert "What tables do we have for orders and revenue?" in body
        assert "The same knowledge, everywhere." in body
        assert "Claude Code" in body
        assert "Cursor" in body
        assert "VS Code" in body
        assert "CLI" in body
        assert "Connect once" in body
        # New prototype chrome: Kai attribution pill, Conversations history
        # panel, and the Keboola footer lockup.
        assert "Operated by" in body and "Kai" in body
        assert "Conversations" in body
        assert "Powered by" in body and "Keboola" in body

    def test_source_pill_zero_by_default(self, seeded_app):
        """analyst1 has zero group grants in the seeded fixture (only the
        canonical system memory domains exist, and those aren't granted to
        anyone yet) -> the pill reads 0 and 0."""
        c = seeded_app["client"]
        resp = c.get("/ask", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200
        assert "using 0 knowledge sources" in resp.text
        assert "0 capabilities from your Stack" in resp.text

    def test_source_pill_reflects_rbac_grant(self, seeded_app):
        """A required data-package grant on the analyst's group bumps N —
        and pluralizes down to the singular "source" at N=1."""
        pkg_id = _make_pkg("ask-landing-pkg", "Ask landing pkg")
        _grant("Everyone", "data_package", pkg_id, requirement="required", users=["analyst1"])
        c = seeded_app["client"]
        resp = c.get("/ask", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200
        assert "using 1 knowledge source " in resp.text
        assert "knowledge sources" not in resp.text

    def test_source_pill_admin_sees_all_packages(self, seeded_app):
        """Admin god-mode counts every data package regardless of grants —
        matches the /catalog Browse admin behavior. The instance seeds a
        fixed set of canonical system memory domains (also god-mode
        visible to admin), so assert the *delta* from adding one package
        rather than an absolute count."""
        import re

        c = seeded_app["client"]
        headers = _auth(seeded_app["admin_token"])
        before = c.get("/ask", headers=headers)
        assert before.status_code == 200
        before_n = int(re.search(r"using (\d+) knowledge",before.text).group(1))

        _make_pkg("ask-landing-pkg-admin", "Ask landing pkg admin")
        after = c.get("/ask", headers=headers)
        assert after.status_code == 200
        after_n = int(re.search(r"using (\d+) knowledge",after.text).group(1))
        assert after_n == before_n + 1

    def test_capabilities_count_pluralization(self, seeded_app, monkeypatch):
        from src import marketplace_filter

        monkeypatch.setattr(
            marketplace_filter,
            "resolve_allowed_plugins",
            lambda conn, user: [{"manifest_name": "demo-plugin"}],
        )
        c = seeded_app["client"]
        resp = c.get("/ask", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        assert "1 capability" in resp.text
        assert "capabilities" not in resp.text

    def test_requires_login(self, seeded_app):
        """Same auth gate as every other authenticated page — unauthenticated
        requests redirect to /login rather than rendering (TestClient
        follows redirects by default, so assert on the pre-redirect hop)."""
        c = seeded_app["client"]
        resp = c.get("/ask", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"].startswith("/login")
