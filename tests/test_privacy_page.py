"""The public privacy statement at `/privacy` (CON-2).

The content already existed at `/how-it-works#privacy` — but that route is
behind `get_current_user`, so the URL answered a signed-out fetch with a login
redirect. That is precisely how a connector directory reads it: both the
Anthropic and the OpenAI submissions fetch the privacy-policy URL without
credentials and treat an unreachable one as an automatic rejection.

So the load-bearing property here is not the wording, it is that the route
answers **unauthenticated**. Everything else on this page can be rewritten;
if these two tests stop holding, the URL silently stops being usable for the
thing it exists for, and nothing else in the suite would notice.
"""

from __future__ import annotations

import pytest


class TestItAnswersWithoutCredentials:
    def test_no_auth_header_still_gets_the_page(self, seeded_app):
        """No Authorization, no cookie, no redirect — 200 with the content."""
        resp = seeded_app["client"].get("/privacy", follow_redirects=False)
        assert resp.status_code == 200, (
            f"got {resp.status_code} — a directory reviewer fetches this URL "
            "signed out, and anything but 200 reads as 'no privacy policy'"
        )
        assert "Where your data goes" in resp.text

    def test_it_does_not_bounce_to_login(self, seeded_app):
        resp = seeded_app["client"].get("/privacy", follow_redirects=False)
        assert "/login" not in resp.headers.get("location", "")


class TestItSaysWhoIsResponsible:
    """A vendor cannot write the controller's policy — the page has to say so."""

    def test_it_names_the_operator_as_responsible(self, seeded_app):
        # Collapse whitespace: the sentence wraps in the template, so matching
        # the raw body would fail on a reflow rather than on a meaning change.
        body = " ".join(seeded_app["client"].get("/privacy").text.lower().split())
        assert "not a substitute" in body
        assert "organization running this instance" in body

    def test_it_carries_the_four_guarantees(self, seeded_app):
        body = seeded_app["client"].get("/privacy").text
        for claim in (
            "You only ever see what you're granted",
            "Query results stay local",
            "Session transcripts are collected",
            "You can go off the record",
        ):
            assert claim in body, f"missing guarantee: {claim!r}"

    def test_a_configured_support_contact_renders(self, seeded_app, monkeypatch):
        """The template checked a bare `instance_support` variable that
        `_build_context` never set — the value only ever reaches the page as
        `config.INSTANCE_SUPPORT`, so this line silently never rendered."""
        monkeypatch.setenv("AGNES_INSTANCE_SUPPORT", "Ping #data-help on Slack")
        body = seeded_app["client"].get("/privacy").text
        assert "Ping #data-help on Slack" in body

    def test_no_support_contact_configured_omits_the_line(self, seeded_app, monkeypatch):
        monkeypatch.delenv("AGNES_INSTANCE_SUPPORT", raising=False)
        body = seeded_app["client"].get("/privacy").text
        assert "Support contact for this instance" not in body


class TestTheOperatorOverride:
    """An operator with their own policy owns the answer at this URL."""

    def test_a_configured_url_redirects_there(self, seeded_app, monkeypatch):
        monkeypatch.setenv("AGNES_PRIVACY_POLICY_URL", "https://example.com/legal/privacy")
        resp = seeded_app["client"].get("/privacy", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "https://example.com/legal/privacy"

    def test_a_blank_setting_falls_back_to_the_built_in_page(self, seeded_app, monkeypatch):
        """Whitespace is not a URL — it must not redirect to nowhere."""
        monkeypatch.setenv("AGNES_PRIVACY_POLICY_URL", "   ")
        resp = seeded_app["client"].get("/privacy", follow_redirects=False)
        assert resp.status_code == 200

    def test_the_override_is_read_per_request_not_at_import(self, seeded_app, monkeypatch):
        """Set it, unset it, and the route follows — an operator changing the
        config must not need a redeploy to take effect."""
        monkeypatch.setenv("AGNES_PRIVACY_POLICY_URL", "https://example.com/p")
        assert seeded_app["client"].get("/privacy", follow_redirects=False).status_code == 302
        monkeypatch.delenv("AGNES_PRIVACY_POLICY_URL")
        assert seeded_app["client"].get("/privacy", follow_redirects=False).status_code == 200


def test_the_how_it_works_anchor_is_still_the_authenticated_one(seeded_app):
    """Guards the distinction this task exists for.

    `/how-it-works#privacy` stays behind auth — it is orientation copy for
    users, not the public policy. If it ever became public the two would drift
    into two answers to the same question.
    """
    resp = seeded_app["client"].get("/how-it-works", follow_redirects=False)
    assert resp.status_code in (302, 307, 401, 403), (
        "/how-it-works answered a signed-out request — the public statement is /privacy"
    )


@pytest.mark.parametrize("token_key", ["analyst_token", "admin_token"])
def test_signed_in_users_get_the_same_page(seeded_app, token_key):
    """No second, member-only variant to keep in step with the public one."""
    resp = seeded_app["client"].get("/privacy", headers={"Authorization": f"Bearer {seeded_app[token_key]}"})
    assert resp.status_code == 200
    assert "Where your data goes" in resp.text
