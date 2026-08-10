"""The agent builder's Preview must not impersonate a working chat box.

The Preview card renders a pill with the agent's name as placeholder text
and a filled circular send button — pixel-for-pixel the app's real chat
composer. It is a static mock: `<span id="ag-pv-ph">` inside a `<div>`,
with no input element and no handler. Clicking it and typing does nothing,
which reads as a broken chat rather than as a preview (observed on a live
instance: the text simply vanished).

The fix keeps it a mock — the builder page cannot run a turn — but stops it
claiming to be interactive: it is marked `aria-hidden` for assistive tech,
carries a non-text cursor, and the card is labelled as a preview in the
markup rather than only by the heading above it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TEMPLATE = Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "agents.html"


@pytest.fixture(scope="module")
def markup() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def _preview_input_markup(markup: str) -> str:
    match = re.search(r"'<div class=\"ag-pv-input\".*?ag-pv-send.*?</div>'", markup, re.S)
    assert match, "preview composer markup not found — did the builder stop rendering it?"
    return match.group(0)


class TestPreviewDoesNotLookInteractive:
    def test_preview_composer_is_hidden_from_assistive_tech(self, markup):
        assert 'aria-hidden="true"' in _preview_input_markup(markup)

    def test_preview_composer_is_still_not_a_real_input(self, markup):
        """If it ever becomes a real composer, this file's premise changes."""
        composer = _preview_input_markup(markup)
        assert "<input" not in composer
        assert "<textarea" not in composer

    def test_preview_composer_does_not_offer_a_text_cursor(self, markup):
        """`cursor: text` over a dead pill is the thing that invites typing."""
        rule = re.search(r"\.ag-pv-input\s*\{([^}]*)\}", markup)
        assert rule, ".ag-pv-input rule not found"
        body = rule.group(1)
        assert "cursor:" in body, "no explicit cursor on the fake composer"
        assert "cursor: text" not in body
        assert "user-select: none" in body

    def test_the_card_says_it_is_a_preview(self, markup):
        """The `<h3>Preview</h3>` heading sits outside the card; on a narrow
        column the card can be all the user sees."""
        assert "ag-pv-note" in markup
