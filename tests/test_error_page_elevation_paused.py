"""The error page finishes the job `admin_elevation_paused` was created for.

`app/auth/access.py` raises a DISTINCT `admin_elevation_paused` detail
instead of a generic 403, and says why in its own comment: "so clients can
offer a 're-enable admin mode' action instead of a generic 403." The HTML
error page never implemented that half. An admin who paused their own
elevation and then clicked an admin link landed on a bare 403 showing the
raw machine string, with Go home / Back as the only actions — the one
control that resolves it lives on `/me/profile`, which the page does not
mention.

Also fixed here: `error.html` was the last template still titling itself
"Data Analyst Portal" while every other page uses `config.INSTANCE_NAME`,
so the browser tab changed brand on the error path.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TEMPLATE = Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "error.html"


@pytest.fixture(scope="module")
def markup() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


class TestTitleUsesInstanceName:
    def test_title_is_not_hardcoded_to_the_old_brand(self, markup):
        assert "Data Analyst Portal" not in markup

    def test_title_uses_the_same_source_as_every_other_page(self, markup):
        title = re.search(r"\{%\s*block title\s*%\}(.*?)\{%\s*endblock\s*%\}", markup, re.S)
        assert title, "no title block"
        assert "config.INSTANCE_NAME" in title.group(1)


class TestElevationPausedGetsItsAction:
    def test_page_offers_re_enabling_admin_mode(self, markup):
        """The action the distinct detail exists to enable."""
        assert "admin_elevation_paused" in markup, "page does not branch on the detail"
        assert "/me/profile" in markup, "no route to the control that fixes it"

    def test_the_action_is_gated_on_that_detail(self, markup):
        """A generic 403 must not advertise an admin-only remedy."""
        assert re.search(r"\{%\s*if .*admin_elevation_paused", markup), (
            "the re-enable affordance is not conditional on the detail"
        )


class TestTheLinkTargetExists:
    def test_profile_carries_the_anchor_the_403_links_to(self):
        """A link to `#admin-mode` is only useful if something has that id.

        The section previously carried `aria-label="Admin mode"` and no id,
        so the anchor would have scrolled nowhere — the same silent dead end
        this change set is fixing elsewhere.
        """
        profile = (Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "profile.html").read_text(
            encoding="utf-8"
        )
        assert 'id="admin-mode"' in profile

        error_page = TEMPLATE.read_text(encoding="utf-8")
        anchors = re.findall(r"/me/profile#([\w-]+)", error_page)
        assert anchors, "error page links to /me/profile without an anchor"
        for anchor in anchors:
            assert f'id="{anchor}"' in profile, f"#{anchor} has no target on the profile page"


class TestRenderedPage:
    def test_paused_admin_sees_the_remedy(self, seeded_app, monkeypatch):
        """End to end: pause elevation, hit an admin page, read the response."""
        monkeypatch.setattr("app.auth.elevation.elevation_paused", lambda: True)
        c = seeded_app["client"]
        c.cookies.set("access_token", seeded_app["admin_token"])
        r = c.get("/admin/server-config", headers={"Accept": "text/html"})

        assert r.status_code == 403
        body = r.text
        assert "/me/profile" in body, "403 page does not point at the fix"
        assert "Data Analyst Portal" not in body

    def test_an_ordinary_403_does_not_offer_it(self, seeded_app):
        """A non-admin hitting an admin page is not told to un-pause anything."""
        c = seeded_app["client"]
        c.cookies.set("access_token", seeded_app["analyst_token"])
        r = c.get("/admin/server-config", headers={"Accept": "text/html"})

        if r.status_code == 403:
            assert "Re-enable admin mode" not in r.text
