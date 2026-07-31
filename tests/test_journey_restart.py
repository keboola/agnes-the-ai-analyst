"""Static-source guard for the journey panel's "Start over" action (#1038).

Once every step is done, "Finish onboarding" disappeared with nothing to
replace it — the panel's only other action, "↻", only relaunches the Stack
coach-mark tour and never resets the checklist itself, so there was no way
back to the start.

No headless browser in CI — this asserts the source contract the way
test_chat_surface_badge.py and test_design_system_contract.py do. The
backend side (PUT /api/chat/journey accepting explicit `false`, which this
button relies on rather than needing a new endpoint) is covered by
tests/test_chat_api.py::test_put_journey_can_reset_explicit_false.
"""

from pathlib import Path

ONBOARDING_JS = Path("app/web/static/js/chat_onboarding.js")


def _js() -> str:
    return ONBOARDING_JS.read_text(encoding="utf-8")


def test_complete_state_renders_a_restart_button():
    js = _js()
    assert 'complete ? "data-journey-restart" : "data-journey-finish-all"' in js
    # The incomplete-state label reads "Skip onboarding" now: the action always
    # ticked all five flags without doing any of them, and "Finish" on the
    # panel's only button-shaped control invited exactly that click. The
    # complete-state half — the reason this test exists — is unchanged.
    assert '${complete ? "Start over" : "Skip onboarding"}' in js


def test_restart_button_resets_all_five_flags_to_false():
    js = _js()
    handler = js.split('el.querySelector("[data-journey-restart]")', 1)[1].split("});\n  }", 1)[0]
    for field in (
        "first_asked",
        "stack_setup_done",
        "explored_stack",
        "catalog_discovered",
        "use_anywhere",
    ):
        assert f"{field}: false" in handler


def test_restart_also_re_arms_the_greeting():
    """#1110 — "Start over" must clear `onboarded` too, otherwise the
    "Hi, I'm Agnes 👋" greeting (gated on `journey.onboarded`) never
    replays alongside the freshly-unchecked checklist."""
    js = _js()
    handler = js.split('el.querySelector("[data-journey-restart]")', 1)[1].split("});\n  }", 1)[0]
    assert "onboarded: false" in handler


def test_restart_reuses_the_existing_patch_journey_helper():
    """No bespoke fetch call — it goes through the same optimistic
    merge-then-PUT path every other journey action uses, so the panel
    re-renders immediately without a page reload."""
    js = _js()
    handler = js.split('el.querySelector("[data-journey-restart]")', 1)[1].split("});\n  }", 1)[0]
    assert "patchJourney({" in handler
