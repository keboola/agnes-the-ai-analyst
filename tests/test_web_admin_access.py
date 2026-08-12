"""The Access surface (`GET /admin/access`) — the third leg of
People → Data → Access.

This URL has had three lives and the tests have to pin the current contract
without losing the reason for the middle one: it was a standalone grant
matrix, was retired into the group detail page's Access tab (grants key on
`group_id`, so the group's own page is where a single group's grants belong),
and returns as the cross-group WORKSPACE plus **Simulate** — two jobs the
per-group tab structurally cannot do.

What matters, and so what is pinned here:

  * it is not a fork — the page reads `/api/admin/access-overview` and writes
    the same `/api/admin/grants` rows as the group tab and the package-side
    Share editor, so the three surfaces cannot disagree;
  * both entry points survive (the group tab is still linked);
  * the legacy `/admin/grants` URL still resolves, carrying `?group=`;
  * the admin gate holds on the page AND on the redirect — a 308 naming an
    internal URL would leak where a surface lives to a non-admin.
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestAccessPage:
    def test_admin_sees_both_tabs(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/admin/access", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        assert "Who can use what" in resp.text
        assert "Simulate a person" in resp.text

    def test_non_admin_is_refused(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/admin/access", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code in (401, 403)

    def test_it_reads_and_writes_the_canonical_grant_apis(self, seeded_app):
        """One storage, three entry points. A page that grew its own endpoint
        (or its own table) is how the group tab and this view would start
        disagreeing about who can use what."""
        c = seeded_app["client"]
        body = c.get("/admin/access", headers=_auth(seeded_app["admin_token"])).text
        assert "/api/admin/access-overview" in body
        assert "/api/admin/grants" in body

    def test_the_group_side_editor_is_still_reachable(self, seeded_app):
        """The per-group tab is not replaced — it carries Members beside
        Access and is where you land from a group. This page links to it."""
        c = seeded_app["client"]
        body = c.get("/admin/access", headers=_auth(seeded_app["admin_token"])).text
        assert "/admin/groups" in body

    def test_tiers_are_worded_as_what_they_do(self, seeded_app):
        """`available`/`required` is the API's vocabulary; an admin reads
        Optional/Automatic. Both must be present — the plain-language label
        for the reader, the system word for the control's title so the two
        vocabularies stay connected."""
        c = seeded_app["client"]
        body = c.get("/admin/access", headers=_auth(seeded_app["admin_token"])).text
        assert "Optional" in body and "Automatic" in body
        assert '"available"' in body and '"required"' in body

    def test_simulate_uses_the_effective_access_endpoint(self, seeded_app):
        """The reason chain is derived from the explicit grant graph the API
        already exposes, not recomputed in the page — recomputing it is how a
        debugging view starts disagreeing with enforcement."""
        c = seeded_app["client"]
        body = c.get("/admin/access", headers=_auth(seeded_app["admin_token"])).text
        assert "effective-access" in body
        assert "memberships" in body

    def test_admin_god_mode_is_stated_not_hidden(self, seeded_app):
        """Admins reach everything regardless of grants (`is_user_admin`
        short-circuits every check). A page about access that does not say so
        invites an admin to conclude their grants are what let them in."""
        c = seeded_app["client"]
        body = c.get("/admin/access", headers=_auth(seeded_app["admin_token"])).text
        assert "Admins can always reach everything" in body


class TestLegacyGrantsUrl:
    def test_it_redirects_to_the_access_page(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get(
            "/admin/grants",
            headers=_auth(seeded_app["admin_token"]),
            follow_redirects=False,
        )
        assert resp.status_code == 308
        assert resp.headers["location"] == "/admin/access"

    def test_it_carries_the_group_deep_link_through(self, seeded_app):
        """The retired matrix accepted `?group=<id>`; the Access page reads it
        to preselect that group, so an old bookmark still lands somewhere
        useful instead of on an arbitrary first group."""
        c = seeded_app["client"]
        resp = c.get(
            "/admin/grants?group=grp-123",
            headers=_auth(seeded_app["admin_token"]),
            follow_redirects=False,
        )
        assert resp.status_code == 308
        assert resp.headers["location"] == "/admin/access?group=grp-123"

    def test_the_redirect_keeps_the_admin_gate(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get(
            "/admin/grants",
            headers=_auth(seeded_app["analyst_token"]),
            follow_redirects=False,
        )
        assert resp.status_code in (401, 403)


class TestAccessIsInTheNav:
    def test_the_access_section_carries_the_page(self):
        """Access is the third intent section — the row must exist, and the
        legacy URL must light it rather than Groups (where it pointed while
        the matrix lived on the group's detail tab)."""
        from app.web.admin_nav import ADMIN_NAV_SECTIONS, resolve_active_href

        access = next((s for s in ADMIN_NAV_SECTIONS if s["key"] == "access"), None)
        assert access is not None, "the Access section is missing from the nav inventory"
        assert [i["href"] for i in access["items"]] == ["/admin/access"]
        assert resolve_active_href("/admin/access") == "/admin/access"
        assert resolve_active_href("/admin/grants") == "/admin/access"

    def test_the_page_renders_the_nav_row_as_active(self, seeded_app):
        c = seeded_app["client"]
        body = c.get("/admin/access", headers=_auth(seeded_app["admin_token"])).text
        # The sidebar's active row is the Access one, and only it.
        assert body.count("admin-nav__link is-active") == 1
        assert 'href="/admin/access"' in body
