"""GET /corporate-memory page rendering — pending banner contract.

The page used to filter `status IN ('approved','mandatory')` with no hint
that a `pending` review queue exists. Operators who configured
`approval_mode='review_queue'` saw an empty page after every collection
run and had no breadcrumb to /corporate-memory/admin. Closes one of
five defects in #176.

Contract:
- Admins see a banner when count(*) WHERE status='pending' > 0,
  with a link to /corporate-memory/admin.
- Non-admins see no change to the page.
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _seed_pending_item(item_id: str = "pending_item_1"):
    from src.db import get_system_db
    from src.repositories.knowledge import KnowledgeRepository

    conn = get_system_db()
    repo = KnowledgeRepository(conn)
    repo.create(
        id=item_id,
        title=f"Pending review item {item_id}",
        content="awaiting admin triage",
        category="workflow",
        status="pending",
    )
    conn.close()


class TestPendingBannerForAdmins:
    def test_admin_sees_pending_banner_when_pending_items_exist(self, seeded_app):
        _seed_pending_item("p_admin_1")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/corporate-memory", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.text
        # Banner must mention the pending count and link to the admin queue.
        assert "pending" in body.lower()
        assert "/corporate-memory/admin" in body

    def test_admin_no_banner_when_no_pending(self, seeded_app):
        # Default seed has zero pending items.
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/corporate-memory", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.text
        # The literal banner copy mentions "awaiting review"; absent when no
        # pending items.
        assert "awaiting review" not in body.lower()


class TestNonAdminBlocked:
    def test_analyst_gets_403_on_corporate_memory(self, seeded_app):
        """Corporate Memory is admin-only — both the nav link and the
        widget are hidden for non-admin in the templates, and the route
        itself rejects with 403. Banner-leakage to non-admin is moot
        because the whole page is gated."""
        _seed_pending_item("p_no_admin_1")
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/corporate-memory", headers=_auth(token))
        assert resp.status_code == 403
