"""The Library's `type='agent'` is called "Agent Template" in the UI (AGT-4).

Two entities were both called "Agent": the personal, runnable agent a user
configures on /agents, and the shareable Library resource that is only a system
prompt. Issue #865 predicted the collision and asked the newer concept to take a
different name; it did not, and the copy spent three places insisting the two
were different things instead. The Library one is now an "Agent Template".

Where the collision actually lives, which is not obvious:

* ``/skills`` (the Builder) is layout-independent — one template for everyone,
  and the type picker is where the old "shareable agent" wording sat.
* ``/agents`` and ``/library`` each serve TWO templates off ``get_ui_layout()``:
  the ``*_legacy.html`` pair for the default ``topnav`` chrome, and the redesign
  pair for ``rail``. The capability picker that rendered a bare ``agent`` tag is
  in the **rail** template only — ``agents_legacy.html`` talks exclusively about
  personal agents, which is correct usage and untouched here. So the rail
  assertions below have to ask for the rail layout to see the code under test.

The rename is DISPLAY ONLY. ``type='agent'`` stays the wire and DB value.
"""

from __future__ import annotations

import pytest


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestBuilderCopy:
    """/skills — one template, every layout."""

    def test_builder_calls_it_an_agent_template(self, seeded_app):
        resp = seeded_app["client"].get("/skills?type=agent", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200
        assert "Agent Template" in resp.text or "agent template" in resp.text.lower()

    def test_the_old_name_is_gone_from_the_builder(self, seeded_app):
        resp = seeded_app["client"].get("/skills?type=agent", headers=_auth(seeded_app["analyst_token"]))
        assert "shareable agent" not in resp.text.lower()

    def test_the_callout_no_longer_argues_with_the_other_agent(self, seeded_app):
        """The callout used to spend a sentence on what this is NOT.

        That sentence existed because the two things shared a name. They no
        longer do, so the callout explains the template instead of disowning
        the personal agent.
        """
        resp = seeded_app["client"].get("/skills?type=agent", headers=_auth(seeded_app["analyst_token"]))
        assert "not one of your" not in resp.text.lower()


class TestRailCapabilityPicker:
    """/agents under the rail layout — where the bare `agent` tag rendered."""

    @pytest.fixture
    def rail_client(self, seeded_app, monkeypatch):
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        return seeded_app["client"]

    def test_capability_type_tag_is_labelled_not_raw(self, rail_client, seeded_app):
        resp = rail_client.get("/agents", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200
        assert "TYPE_LABEL" in resp.text, (
            "the capability row still renders the raw store `type`; a Library "
            "agent template shows up as the bare word 'agent' inside the page "
            "where the reader is building an agent"
        )
        assert "agent: 'Agent Template'" in resp.text


class TestContractUnchanged:
    """The rename must not have leaked past the display layer."""

    def test_grant_projection_relabels_without_renaming_the_kind(self):
        """`/admin/access` groups by the raw `type`; only the heading changed."""
        import inspect

        import app.resource_types as rt

        body = inspect.getsource(rt)
        assert '"agent": "Agent Templates"' in body
        # The dict is still KEYED on the wire value.
        assert '"agent":' in body

    def test_bundle_description_mentions_templates(self):
        from src.marketplace_filter import BUNDLE_DESCRIPTION

        assert "agent template" in BUNDLE_DESCRIPTION.lower()

    def test_store_entity_type_value_is_untouched(self, seeded_app):
        """A client filtering on `type=agent` must keep working."""
        resp = seeded_app["client"].get("/api/marketplace/items?limit=1", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200
        # The wire vocabulary is unchanged — nothing renamed the value itself.
        assert "agent_template" not in resp.text, "the display rename leaked into the API payload"
