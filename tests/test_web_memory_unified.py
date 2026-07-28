"""GET /corporate-memory — unified Browse / My Stack with domain cards.

Task 8.4 of the v49 plan. The top-level page is now a Browse of memory
domains; the per-item richness moves to /memory/d/<slug> (Task 8.5).
"""

from __future__ import annotations

import uuid


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _make_domain(slug: str = "qa", name: str = "QA", *,
                 with_item: bool = True) -> str:
    """Create a memory domain and (by default) attach one approved item to
    it. Empty domains are hidden from /corporate-memory by design — a
    domain with no items has nothing for an analyst to opt-into — so tests
    asserting visibility must seed at least one item. Pass
    ``with_item=False`` to test the empty-hidden contract explicitly."""
    from src.db import get_system_db
    from src.repositories.memory_domains import MemoryDomainsRepository
    from src.repositories.knowledge import KnowledgeRepository

    conn = get_system_db()
    try:
        domain_id = MemoryDomainsRepository(conn).create(
            slug=slug, name=name, description=f"{name} desc",
            icon="🎯", color="#dcfce7", created_by="test",
        )
        if with_item:
            kr = KnowledgeRepository(conn)
            item_id = str(uuid.uuid4())
            kr.create(
                id=item_id,
                title=f"{name} starter item",
                content="seeded for visibility test",
                category="convention",
                domain=slug,
                source_type="manual",
                source_user="test",
            )
            kr.update_status(item_id, "approved")
        return domain_id
    finally:
        conn.close()


def _grant(group_name: str, resource_id: str, requirement: str = "available",
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
            [str(uuid.uuid4()), group_id, resource_id, requirement],
        )
    finally:
        conn.close()


class TestMemoryUnifiedPage:
    def test_admin_sees_browse_and_my_stack_tabs(self, seeded_app):
        _make_domain("qa-domain-1", "QA")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/corporate-memory", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.text
        assert "Browse" in body
        assert "My Stack" in body
        # Domain card visible to admin (god-mode).
        assert "QA" in body

    def test_analyst_with_required_domain_grant_sees_card(self, seeded_app):
        dom_id = _make_domain("eng", "Engineering Memory")
        _grant("Everyone", dom_id, requirement="required", users=["analyst1"])
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/corporate-memory", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.text
        assert "Engineering Memory" in body
        assert "is-required" in body

    def test_analyst_no_grants_sees_empty_state(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/corporate-memory", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.text
        # Either explicit "ask your admin" or "no memory domains" empty banner.
        assert "ask your admin" in body.lower() or "no memory" in body.lower()
