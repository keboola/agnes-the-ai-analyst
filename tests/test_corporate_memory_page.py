"""GET /corporate-memory page rendering — pending banner contract.

The page used to filter `status IN ('approved','mandatory')` with no hint
that a `pending` review queue exists. Operators who configured
`approval_mode='review_queue'` saw an empty page after every collection
run and had no breadcrumb to /admin/corporate-memory. Closes one of
five defects in #176.

Contract:
- /corporate-memory is user-facing (get_current_user) — any authenticated
  user can read it.
- Admins see a banner when count(*) WHERE status='pending' > 0,
  with a link to the admin review queue at /admin/corporate-memory.
- Non-admins reach the page but the pending banner is suppressed
  server-side (`pending_review_count` zeroed for non-admins).
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
        assert "/admin/corporate-memory" in body

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


class TestNonAdminAccess:
    def test_analyst_can_access_corporate_memory(self, seeded_app):
        """Curated Memory is user-facing — the route runs on
        get_current_user (parity with the /api/memory/* endpoints), so
        any authenticated analyst can read the page. The pending-review
        banner is the only admin-only affordance and is suppressed
        server-side: `pending_review_count` is zeroed for non-admins."""
        _seed_pending_item("p_no_admin_1")
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/corporate-memory", headers=_auth(token))
        assert resp.status_code == 200
        # Admin-only pending banner must not leak to non-admins, even
        # though a pending item exists.
        assert "awaiting review" not in resp.text.lower()


class TestAdminGroupsContract:
    def test_admin_page_renders_groups_as_array_not_dict(self, seeded_app):
        """`/admin/corporate-memory` must serialize `groups` as a JS array
        of `{name, members_count}` rows. Earlier the route passed the
        `corporate_memory.groups` YAML config (a dict, default `{}`),
        so `GROUPS.map(...)` inside `renderItemCard` threw
        `{}.map is not a function` and the page surfaced a misleading
        "Error loading pending items" banner over a perfectly valid
        pending payload. Bug was dormant because `renderItemCard` only
        runs when ≥1 pending item exists. This test seeds one pending
        item and asserts the array shape so the regression can't return."""
        _seed_pending_item("groups_shape_1")
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/admin/corporate-memory", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.text
        # JS literal must be an array — even when empty it is `[]`, never `{}`.
        assert "const GROUPS = [" in body, (
            "groups must serialize as a JS array of {name, members_count}; "
            "a `{` prefix means we regressed to passing a dict and "
            "renderItemCard's GROUPS.map(...) will crash at runtime."
        )
