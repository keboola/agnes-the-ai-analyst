"""GET /memory/d/<slug> — per-domain drill-down (Task 8.5 of v49 plan).

Preserves the per-item richness from the legacy /corporate-memory page:
title + content (markdown), votes + score, contributors, tags, category
badge, confidence/source/sensitivity, admin Edit button, Dismiss /
Mark-personal toggles, Required badge on `is_required` items.
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
            slug=slug, name=name, description=f"{name} desc",
            icon="🎯", color="#dcfce7", created_by="test",
        )
    finally:
        conn.close()


def _make_item(item_id: str, title: str, domain_id: str,
               is_required: bool = False, status: str = "approved"):
    from src.db import get_system_db
    from src.repositories.knowledge import KnowledgeRepository

    conn = get_system_db()
    try:
        repo = KnowledgeRepository(conn)
        repo.create(
            id=item_id, title=title, content=f"# {title}\n\nbody",
            category="workflow", status=status, is_required=is_required,
            source_user="contrib@example.com",
        )
        # Wire into the junction so the domain drill-down picks it up.
        conn.execute(
            "INSERT INTO knowledge_item_domains(item_id, domain_id, added_by) "
            "VALUES (?, ?, 'test')",
            [item_id, domain_id],
        )
    finally:
        conn.close()


def _grant_domain(group_name: str, domain_id: str, requirement: str = "available",
                  users: list[str] | None = None):
    from src.db import get_system_db
    from src.repositories.user_group_members import UserGroupMembersRepository

    conn = get_system_db()
    try:
        gid_row = conn.execute(
            "SELECT id FROM user_groups WHERE name = ?", [group_name]
        ).fetchone()
        if not gid_row:
            return
        group_id = gid_row[0]
        if users:
            for u in users:
                try:
                    UserGroupMembersRepository(conn).add_member(u, group_id, source="test")
                except Exception:
                    pass
        conn.execute(
            "INSERT INTO resource_grants(id, group_id, resource_type, resource_id, "
            "requirement, assigned_at, assigned_by) "
            "VALUES (?, ?, 'memory_domain', ?, ?, CURRENT_TIMESTAMP, 'test')",
            [str(uuid.uuid4()), group_id, domain_id, requirement],
        )
    finally:
        conn.close()


class TestMemoryDomainDetail:
    def test_unknown_slug_returns_404(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/memory/d/does-not-exist", headers=_auth(token))
        assert resp.status_code == 404

    def test_admin_can_view_any_domain(self, seeded_app):
        dom_id = _make_domain("ops-admin", "Ops")
        _make_item("ops_item_1", "Ops runbook", dom_id)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/memory/d/ops-admin", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.text
        assert "Ops" in body
        # Item title + back link present.
        assert "Ops runbook" in body
        # Topnav (default) layout → standalone memory page is the browse home.
        assert 'href="/corporate-memory"' in body

    def test_back_link_targets_unified_catalog_under_rail(self, seeded_app, monkeypatch):
        # Under the rail IA (#896) /corporate-memory is orphaned (nothing in
        # the rail nav links to it); the back-link must return to the unified
        # Catalog's Memory tab instead so the user stays in the new IA.
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        dom_id = _make_domain("ops-rail", "Ops Rail")
        _make_item("ops_rail_item_1", "Ops rail runbook", dom_id)
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/memory/d/ops-rail", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.text
        assert 'href="/catalog?kind=memory"' in body
        assert 'href="/corporate-memory"' not in body

    def test_analyst_no_grant_blocked(self, seeded_app):
        _make_domain("locked-dom", "Locked")
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/memory/d/locked-dom", headers=_auth(token))
        assert resp.status_code == 403

    def test_required_item_shows_required_badge(self, seeded_app):
        dom_id = _make_domain("req-dom", "Required things")
        _make_item("req_item_1", "Critical SOP", dom_id, is_required=True)
        _grant_domain("Everyone", dom_id, requirement="available",
                      users=["analyst1"])
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/memory/d/req-dom", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.text
        assert "Critical SOP" in body
        # Required badge on the item row.
        assert "Required" in body
