"""Template tests for the Catalog Browse grid's card metadata — the owner
byline, tag chips and the org/new claims.

Originally written against ``macros/_stack_card.html`` (v56), which rendered
Data Packages on ``/catalog``. The Catalog grid moved to
``macros/_catalog_card.html`` — the one component now shared by every kind,
server-rendered and client-hydrated alike — and ``_stack_card.html`` is left
with a single consumer, ``corporate_memory.html``.

That component swap is NOT wave-0 work; it landed with the Library/Catalog
rework. Wave 0 only made the redesigned path the only path, which is what
exposed these tests: they had been asserting the classic template all along.

What the new card renders for a data package is the owner byline
(``Curated by <name>``, from ``curator``) and the tag chips (``.cc-tag``); both
are asserted below. It renders no org/new claim — ``_catalog_card_data()``
leaves ``publisher`` unset for this kind — so those tests were retired rather
than re-pointed at markup that does not exist. See the note where they were.

Runs with stack auto-membership OFF (see the autouse fixture). Wave 0
(2026-08) made auto-membership the default, and under it a granted package
is in the caller's stack the instant it is granted — so the Catalog's Data
grid, which offers only what you do NOT have, is empty by construction and
there is no card to assert anything about. The classic subscribe model is a
fully supported explicit opt-out and is the mode in which this grid has rows.
"""

from __future__ import annotations

import uuid

import pytest

from src.db import get_system_db


@pytest.fixture(autouse=True)
def _classic_membership(monkeypatch):
    """The Catalog Data grid only has rows under the subscribe model."""
    monkeypatch.setenv("AGNES_STACK_AUTO_MEMBERSHIP", "0")


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
        # The card's eyebrow carries the owner as a curator byline.
        assert "Curated by Jane" in body

    def test_omits_owner_chip_when_unset(self, seeded_app):
        _seed_pkg_for_grid()
        r = seeded_app["client"].get(
            "/catalog",
            headers=_auth(seeded_app["analyst_token"]),
        )
        # No byline for cards with no owner set.
        assert "Curated by" not in r.text

    def test_renders_tag_chips_on_card(self, seeded_app):
        """Tag chips survived the component swap.

        `_catalog_card_data()` concatenates the auto-derived source-type pills
        with the admin-authored tags into the single `tags` field
        (`app/web/router.py`), and `macros/_catalog_card.html` renders the first
        three as `.cc-tag` with a `+N` overflow. Asserted on the chip markup,
        not just the words — the tag names also appear in the card's
        `data-search` attribute, so a bare substring check would pass on a
        build that dropped the chips entirely.
        """
        _seed_pkg_for_grid(tags=["Finance", "Revenue", "Margin", "Bookings"])
        r = seeded_app["client"].get(
            "/catalog",
            headers=_auth(seeded_app["analyst_token"]),
        )
        body = r.text
        assert 'class="cc-tag"' in body, "tag chips are not rendering on the catalog card"
        # First three tags rendered; the 4th collapses into the +N overflow.
        for tag in ("Finance", "Revenue", "Margin"):
            assert f'<span class="cc-tag">{tag}</span>' in body
        assert 'class="cc-tag cc-tag--more">+1<' in body, "the 4th tag must collapse into +N"


    # The whole `TestCardBadges` class was here. It asserted
    # `_stack_card.html`'s curated/new badge row on /catalog. The Catalog card
    # is `_catalog_card.html` now and renders no org/new claim for a data
    # package: `_catalog_card_data()` leaves `publisher` unset, so there is no
    # trust marker on this grid to assert. (Tags DO come through — the test
    # above covers them.)
    #
    # Not re-pointed onto /corporate-memory, the one page still rendering
    # `_stack_card.html`: under the rail that route 302s into the Library's
    # Memory band unless the band would be empty, so reaching the macro would
    # mean constructing the empty-band case — a fixture that tests the redirect
    # guard, not the card.
    #
    # COVERAGE GAP, deliberately recorded rather than papered over: the
    # provenance/trust rendering on the live Catalog card has no test of its
    # own. Its tag rendering does — see above.
