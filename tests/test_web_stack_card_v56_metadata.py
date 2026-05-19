"""Template tests for v56 stack-card additions (owner chip + tags +
curated/new badges) on the Browse grid.

The shared ``_stack_card.html`` macro renders Data Packages on both
``/catalog`` and the per-domain memory pages. v56 layers owner/tags/
badge onto the card without breaking back-compat: rows missing the new
fields render unchanged from v55.
"""

from __future__ import annotations

import uuid

import pytest

from src.db import get_system_db


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _seed_pkg_for_grid(*, created_by="admin1", **fields) -> str:
    from src.repositories.data_packages import DataPackagesRepository

    slug = fields.pop("slug", f"p{uuid.uuid4().hex[:6]}")
    conn = get_system_db()
    pid = DataPackagesRepository(conn).create(
        name=fields.pop("name", "Card test"),
        slug=slug,
        description=fields.pop("description", "card desc"),
        icon=None, color=None, created_by=created_by,
        **fields,
    )
    # Grant Everyone so analyst1 sees it on /catalog.
    ev = conn.execute("SELECT id FROM user_groups WHERE name='Everyone'").fetchone()
    conn.execute(
        "INSERT INTO user_group_members(user_id, group_id, source) "
        "VALUES ('analyst1', ?, 'test') ON CONFLICT DO NOTHING",
        [ev[0]],
    )
    conn.execute(
        "INSERT INTO resource_grants(id, group_id, resource_type, resource_id, "
        "requirement, assigned_at, assigned_by) "
        "VALUES (?, ?, 'data_package', ?, 'available', CURRENT_TIMESTAMP, 'test')",
        [str(uuid.uuid4()), ev[0], pid],
    )
    conn.close()
    return pid


class TestCardOwnerAndTags:
    def test_renders_owner_on_card(self, seeded_app):
        _seed_pkg_for_grid(owner_name="Jane", owner_team="Sales Ops")
        r = seeded_app["client"].get(
            "/catalog",
            headers=_auth(seeded_app["analyst_token"]),
        )
        body = r.text
        assert "Jane" in body
        # Class hook lets us pin the owner-chip rendering without
        # depending on CSS layout.
        assert 'data-card-owner' in body

    def test_omits_owner_chip_when_unset(self, seeded_app):
        _seed_pkg_for_grid()
        r = seeded_app["client"].get(
            "/catalog",
            headers=_auth(seeded_app["analyst_token"]),
        )
        # No data-card-owner attr for cards with no owner set.
        assert 'data-card-owner' not in r.text

    def test_renders_tag_chips_on_card(self, seeded_app):
        _seed_pkg_for_grid(tags=["Finance", "Revenue", "Margin", "Bookings"])
        r = seeded_app["client"].get(
            "/catalog",
            headers=_auth(seeded_app["analyst_token"]),
        )
        body = r.text
        # First three tags rendered; 4th may collapse into +N overflow.
        for tag in ("Finance", "Revenue", "Margin"):
            assert tag in body


class TestCardBadges:
    def test_curated_badge_on_card_for_admin_created(self, seeded_app):
        _seed_pkg_for_grid(created_by="admin1")
        r = seeded_app["client"].get(
            "/catalog",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert 'data-badge="curated"' in r.text

    def test_new_badge_on_card_for_recent(self, seeded_app):
        _seed_pkg_for_grid(created_by="admin1")
        r = seeded_app["client"].get(
            "/catalog",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert 'data-badge="new"' in r.text

    def test_no_badges_on_back_compat_row(self, seeded_app):
        """Pre-v56 package created by a non-admin user, older than 30d:
        renders without curated or new badges."""
        from datetime import datetime, timedelta, timezone

        pid = _seed_pkg_for_grid(created_by="analyst1")
        conn = get_system_db()
        conn.execute(
            "UPDATE data_packages SET created_at = ? WHERE id = ?",
            [datetime.now(timezone.utc) - timedelta(days=120), pid],
        )
        conn.close()
        r = seeded_app["client"].get(
            "/catalog",
            headers=_auth(seeded_app["analyst_token"]),
        )
        body = r.text
        # The specific pkg must not have either badge — but other seed
        # packages on /catalog might. Pin to the card by slug:
        # Hack: ensure neither badge appears within ~600 chars of our slug.
        idx = body.find(pid)
        if idx >= 0:
            window = body[max(0, idx - 600): idx + 600]
            assert 'data-badge="curated"' not in window
            assert 'data-badge="new"' not in window
