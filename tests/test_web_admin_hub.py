"""Admin hub page (GET /admin).

The canonical landing page for instance administration — a settings-style
index of every /admin surface, grouped by domain. The header's Admin mega-menu
links here. Gated by require_admin like every other /admin route; this suite
asserts the gate + the domain cards render.
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestAdminHub:
    def test_admin_sees_hub_with_domain_cards(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/admin", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        body = resp.text
        # Domain groupings present.
        for label in (
            "Activity Center",
            "Users &amp; Access",
            "Data Packages",
            "Sources",
            "Agent Experience",
            "Server",
            "Documentation",
        ):
            assert label in body, f"missing admin-hub domain: {label}"
        # Representative deep links into the gated surfaces.
        for href in ("/admin/users", "/admin/sync", "/admin/server-config"):
            assert f'href="{href}"' in body

    def test_non_admin_gets_403(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/admin", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 403
