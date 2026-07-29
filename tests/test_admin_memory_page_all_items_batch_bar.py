"""GET /admin/corporate-memory page — All Items tab batch bar (issue #129).

Follow-up to #62 / PR #126 which shipped the bulk-edit batch bar in the
Review tab only. This test guards the symmetric bar on the All Items tab:

- batch-bar block visible on page render (regardless of pending count)
- the bulk-edit actions ship with distinct ``*BtnAll`` IDs so they don't
  collide with the Review tab's bare-ID buttons
- moderation actions (Approve / Reject / Revoke / Mark required) are
  present too: the original issue-#129 "status changes stay in Review"
  scope decision was revised once search landed on All Items, turning it
  into the find-and-moderate surface
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestAllItemsBatchBar:
    def test_admin_page_renders_all_items_batch_bar(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/admin/corporate-memory", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.text

        # All five bulk-edit buttons present with the All-suffix IDs the JS
        # plumbing (`updateSelectionCount('all')`) toggles.
        for btn_id in (
            "batchMoveCategoryBtnAll",
            "batchMoveDomainBtnAll",
            "batchAddTagBtnAll",
            "batchRemoveTagBtnAll",
            "batchSetAudienceBtnAll",
        ):
            assert f'id="{btn_id}"' in body, f"missing button id={btn_id}"

        # Select-all checkbox + count span scoped to All Items.
        assert 'id="selectAllAll"' in body
        assert 'id="selectedCountAll"' in body
        assert "toggleSelectAll('all')" in body

    def test_all_items_bar_has_moderation_actions(self, seeded_app):
        """All Items carries the full moderation set alongside bulk-edit.

        Inverts the original issue-#129 guard (Approve/Reject Review-only):
        with search on the tab, All Items is the find-and-moderate surface.
        Revoke / Mark required exist only here — they act on approved items,
        which the pending-only Review queue never shows.
        """
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/admin/corporate-memory", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.text

        assert 'id="batchApproveBtn"' in body  # Review tab keeps its own
        for btn_id in (
            "batchApproveBtnAll",
            "batchRejectBtnAll",
            "batchRevokeBtnAll",
            "batchRequireBtnAll",
        ):
            assert f'id="{btn_id}"' in body, f"missing button id={btn_id}"
        # Revoke / Mark required stay All-Items-only — no bare-ID variants
        # in the Review bar (pending items can't be revoked/required-toggled
        # before approval).
        assert 'id="batchRevokeBtn"' not in body.replace('id="batchRevokeBtnAll"', "")
        assert 'id="batchRequireBtn"' not in body.replace('id="batchRequireBtnAll"', "")

    def test_browse_tab_omits_row_checkbox(self, seeded_app):
        """Regression guard for the adversarial-review finding: widening
        ``renderItemCard``'s checkbox gate from "Review-only" to "Review or
        All" must NOT also render the checkbox in the Browse tab — Browse
        has no batch bar of its own, so an orphan checkbox there would
        fire ``updateSelectionCount('all')`` against an invisible tab and
        confuse the UX.

        The renderItemCard signature is now a ``mode`` enum
        (``'review' | 'all' | 'browse'``); the Browse path passes
        ``'browse'`` and the function early-returns no checkbox markup
        for that mode. Both invariants are checked at template level so
        a future refactor can't silently regress either.
        """
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/admin/corporate-memory", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.text

        # Browse-tab call site uses the 'browse' mode literal.
        assert "renderItemCard(it, idx, 'browse')" in body
        # And renderItemCard's checkbox is gated on `mode === 'browse'` so
        # the input element is omitted entirely on that branch.
        assert "mode === 'browse' ? ''" in body
