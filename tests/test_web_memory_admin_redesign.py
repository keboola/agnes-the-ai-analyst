"""/admin/corporate-memory — moderation queue redesign (Task 8.6).

Visual parity with /memory/d/<slug> per design decision D17 of the v49
plan: items render with the SAME ``.memory-item`` shape used on the
user-facing drill-down, with an admin-specific
``.memory-item__admin-actions`` row for moderation buttons.

The redesign keeps the existing JS hooks intact (``adminAction()``,
``showMandateForm()``, ``openEditItemModal()`` — wired by the templates,
asserted indirectly here) so the moderation flow itself is unchanged;
only the visual layer differs.
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_admin_memory_emits_memory_item_layout(seeded_app):
    """Page exposes the .memory-item / .memory-item__* class set and
    the admin-actions extension so the JS-rendered cards inherit the
    user-facing drill-down's visual shape."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    resp = c.get("/admin/corporate-memory", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.text

    # CSS for the unified shape exists.
    assert ".memory-item.is-required" in body
    assert ".memory-item__header" in body
    assert ".memory-item__title" in body
    assert ".memory-item__badges" in body
    assert ".memory-item__content" in body
    assert ".memory-item__footer" in body
    assert ".memory-item__admin-actions" in body
    # Badge classes mirror /memory/d/<slug>.
    assert ".badge--required" in body
    assert ".badge--category" in body
    assert ".badge--source" in body
    assert ".badge--status" in body


def test_admin_memory_render_emits_admin_actions_row(seeded_app):
    """JS renderer emits the admin actions row alongside the unified
    card shape. Smoke-level — we look at the renderer source in the
    template body since the actual rendering happens client-side."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    resp = c.get("/admin/corporate-memory", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.text

    # The new card emits a top-level <div class="memory-item …">.
    assert 'class="memory-item' in body
    # Admin actions row class set present in renderer.
    assert "memory-item__admin-actions" in body
    # All four moderation hooks still wired — preserved across the redesign.
    assert "adminAction('approve'" in body
    assert "adminAction('reject'" in body
    assert "showMandateForm(" in body
    assert "openEditItemModal(" in body


def test_admin_memory_renderer_emits_status_and_required_badges(seeded_app):
    """Status badge + Required badge rendering present in the JS card."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    resp = c.get("/admin/corporate-memory", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.text

    # Required badge gated on is_required (or status === 'mandatory').
    assert "badge--required" in body
    assert "Required" in body
    # Status badge for non-approved items.
    assert "badge--status" in body
    # is_required modifier on the outer card.
    assert "is-required" in body


def test_admin_memory_renderer_emits_votes_contributors_tags(seeded_app):
    """The footer surfaces votes / contributors / tags — the per-item
    richness preserved from the user-facing drill-down."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    resp = c.get("/admin/corporate-memory", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.text

    assert "memory-item__votes" in body
    assert "memory-item__contributors" in body
    assert "memory-item__tags" in body
    # Vote icons explicitly emitted by the renderer.
    assert "▲ " in body or "▲" in body
    assert "▼ " in body or "▼" in body
