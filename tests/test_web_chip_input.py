"""chip-input component — smoke / wiring tests (Task 8.7).

We don't run a real browser here; full keyboard + dropdown semantics need
Playwright (documented as a follow-up). What this file verifies:

* The asset is reachable via the static mount (no 404).
* The script declares the expected `data-*` API surface so admin templates
  pointing at it don't drift silently.
"""

from __future__ import annotations


def test_chip_input_js_served_via_static(seeded_app):
    """GET /static/js/components/chip-input.js must return 200 — admin
    templates load the component from this path with a versioned cache
    buster."""
    c = seeded_app["client"]
    resp = c.get("/static/js/components/chip-input.js")
    assert resp.status_code == 200
    body = resp.text
    # Public API contract.
    assert "addChip" in body
    assert "chip-create" in body
    assert "data-source-url" in body
    assert "data-allow-create" in body
    # Keyboard handling.
    assert "ArrowDown" in body
    assert "ArrowUp" in body
    assert "Escape" in body
    assert "Backspace" in body
    # a11y attributes.
    assert "aria-activedescendant" in body
    assert 'role="option"' in body
    assert 'aria-autocomplete' in body
