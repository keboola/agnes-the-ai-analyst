"""The builder's Schedules panel (agent-schedules UI, fast follow to #1404).

Template-level guards in the style of `test_agents_preview_is_not_a_chat.py`:
the panel is fetch-driven client JS, so what a test can pin is the markup and
JS the page ships — the API behavior itself is covered end-to-end in
`tests/test_agent_schedules_api.py` and is deliberately NOT re-tested here.

What must hold:

- the panel exists as a numbered builder section wired to the owner CRUD
  routes (`/api/v1/agents/{slug}/schedules`);
- `backlogged` gets a DISTINCT warning treatment — it is the one status that
  means "no worker is claiming agent_response jobs", which an owner must
  notice rather than skim past as just another grey label;
- the create form says a new schedule does not fire immediately (the cadence
  anchors at creation) and names the schedule grammar including the `cron `
  prefix footgun — both straight from the design doc;
- every server error code the API can return has an owner-readable message;
- user-authored values (names, cadences, prompts, skill names) are escaped
  before landing in innerHTML.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TEMPLATE = Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "agents.html"


@pytest.fixture(scope="module")
def markup() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


class TestPanelExists:
    def test_schedules_is_a_builder_section(self, markup):
        assert re.search(r"section\('schedules',\s*\d+,\s*'Schedules'", markup)

    def test_crud_rides_the_agent_schedules_api(self, markup):
        assert "/api/v1/agents/" in markup
        assert "/schedules" in markup
        # Slug is user-influenced (derived from the agent name) — it must be
        # URL-encoded on the way into the path.
        assert "encodeURIComponent(a.slug)" in markup

    def test_disabled_instance_is_named_not_generic(self, markup):
        """The 403 {"kind": "agent_profiles_disabled"} answer must say what is
        off and who can change it, not render a retry-me error."""
        assert "agent profiles are disabled" in markup


class TestBackloggedIsAWarning:
    def test_backlogged_has_its_own_status_class(self, markup):
        assert "ag-sch-status--backlogged" in markup

    def test_backlogged_wears_the_warn_accent(self, markup):
        rule = re.search(r"\.ag-sch-status--backlogged\s*\{([^}]*)\}", markup)
        assert rule, "backlogged status rule not found"
        body = rule.group(1)
        assert "--ds-accent-warn-ink" in body
        assert "--ds-accent-warn-bg" in body

    def test_backlogged_explains_the_stuck_worker(self, markup):
        """The badge's title must say what backlogged MEANS — the previous run
        is still queued because nothing is claiming agent_response jobs."""
        assert "no worker is picking up agent_response jobs" in markup

    def test_the_other_statuses_do_not_share_the_warn_treatment(self, markup):
        for cls in ("ag-sch-status--enqueued", "ag-sch-status--failed"):
            rule = re.search(r"\." + re.escape(cls) + r"\s*\{([^}]*)\}", markup)
            assert rule, f"{cls} rule not found"
            assert "warn" not in rule.group(1)


class TestCreateFormCopy:
    def test_says_a_new_schedule_does_not_fire_immediately(self, markup):
        assert "doesn’t fire immediately" in markup
        assert "next cadence tick" in markup

    def test_grammar_hint_names_all_three_forms_and_utc(self, markup):
        assert "every 30m" in markup
        assert "daily 07:00" in markup
        assert "cron 0 7 * * 1-5" in markup
        assert "UTC" in markup

    def test_grammar_hint_names_the_cron_prefix_footgun(self, markup):
        assert re.search(r"literal <code>cron\s", markup)

    def test_every_server_error_code_has_a_friendly_message(self, markup):
        """The API's documented error codes — each must map to something a
        person can act on. A new code landing without a message here falls
        back to the server text, which is acceptable but worth noticing."""
        err_map = re.search(r"var SCHED_ERR = \{(.*?)\};", markup, re.S)
        assert err_map, "SCHED_ERR map not found"
        for code in (
            "invalid_name",
            "invalid_schedule",
            "invalid_prompt",
            "invalid_enabled",
            "schedule_name_taken",
            "schedule_limit",
        ):
            assert code + ":" in err_map.group(1), f"no friendly message for {code}"


class TestSkillAwareCreation:
    def test_skill_dropdown_exists_and_reads_the_v2_skills_endpoint(self, markup):
        assert "data-ag-sch-skill" in markup
        assert "/api/v2/marketplace/skills" in markup

    def test_selecting_a_skill_templates_the_prompt(self, markup):
        assert "'Run the ' + label + ' skill'" in markup

    def test_skills_from_the_agents_own_plugins_are_grouped_first(self, markup):
        """The split keys off the capability picker's id vocabulary
        (`curated-{marketplace_id}/{plugin_name}`) against the agent's picked
        plugins — the closest client-side stand-in for the profile-side
        plugins_mode='selected' scope, which has no read API."""
        assert "'curated-' + s.marketplace_id + '/' + s.plugin_name" in markup
        assert "From this agent" in markup


class TestUserInputIsEscaped:
    """Schedule names/cadences/prompts and skill names/plugins are user (or
    curator) authored and land in innerHTML — every interpolation must ride
    the page's esc() helper."""

    def test_schedule_row_escapes_name_cadence_and_prompt(self, markup):
        row = re.search(r"function scheduleRows\(\)(.*?)\n  \}", markup, re.S)
        assert row, "scheduleRows not found"
        body = row.group(0)
        for expr in ("esc(s.name)", "esc(s.schedule)", "esc(s.prompt)", "esc(s.id)"):
            assert expr in body, f"{expr} missing — raw interpolation into innerHTML?"

    def test_skill_options_escape_names_and_keys(self, markup):
        opts = re.search(r"var opt = function \(s\)(.*?)\n    \};", markup, re.S)
        assert opts, "skill option renderer not found"
        body = opts.group(0)
        assert "esc(key)" in body
        assert "esc(s.name || s.skill_name)" in body
        assert "esc(s.plugin_name)" in body

    def test_form_replays_typed_values_through_esc(self, markup):
        form = re.search(r"function scheduleFormHtml\(a\)(.*?)\n  \}", markup, re.S)
        assert form, "scheduleFormHtml not found"
        body = form.group(0)
        for expr in ("esc(schedForm.name)", "esc(schedForm.schedule)", "esc(schedForm.prompt)"):
            assert expr in body

    def test_error_messages_render_via_textcontent_not_innerhtml(self, markup):
        set_err = re.search(r"function setSchedErr\(msg\)(.*?)\n  \}", markup, re.S)
        assert set_err, "setSchedErr not found"
        assert "textContent" in set_err.group(0)
        assert "innerHTML" not in set_err.group(0)
