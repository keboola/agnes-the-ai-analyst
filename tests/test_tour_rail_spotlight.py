"""Static-source guards for tour steps 2-3, which spotlight rail nav items
(#nav-artefacts, #nav-catalog) — the two anchors that live inside the rail's
own stacking context.

No headless browser in CI — these assert the source contract the way
test_chat_surface_badge.py and test_design_system_contract.py do.

Two independent defects on the same two steps:

1. `.tour-spotlight`'s z-index only outranks the scrim if the spotlighted
   element isn't inside another positioned, z-indexed ancestor. The rail is
   `position: fixed; z-index: 40` (rail.css), which creates exactly such a
   stacking context — the spotlighted nav item stayed dimmed under the scrim
   because its z-index was being resolved inside the rail's own context, not
   against the root-level scrim.
2. Below 1024px the rail becomes a wrapping top bar but kept the column
   layout's `overflow: visible` and each nav item's `width: 100%` — both of
   which let the bar overflow the viewport horizontally, and `scrollIntoView`
   would then drag the whole page sideways to center an anchor past the edge.
"""

from pathlib import Path

TOUR_JS = Path("app/web/static/js/tour.js")
TOUR_CSS = Path("app/web/static/css/tour.css")
RAIL_CSS = Path("app/web/static/css/rail.css")


def _js() -> str:
    return TOUR_JS.read_text(encoding="utf-8")


def _tour_css() -> str:
    return TOUR_CSS.read_text(encoding="utf-8")


def _rail_css() -> str:
    return RAIL_CSS.read_text(encoding="utf-8")


# --- Stacking-context escape ---------------------------------------------


def test_tour_finds_and_lifts_a_stacking_context_ancestor():
    js = _js()
    assert "_findStackingContextAncestor" in js
    assert "cs.position !== 'static' && cs.zIndex !== 'auto'" in js
    assert "classList.add('tour-lifts-ancestor')" in js


def test_lifted_ancestor_is_cleaned_up_on_step_change_and_tour_end():
    """The class must not leak onto the rail permanently — it's removed both
    when advancing to a step with a different anchor and when the tour ends
    (Escape, "I'll explore on my own", finishing, etc.)."""
    js = _js()
    assert js.count("classList.remove('tour-lifts-ancestor')") >= 2


def test_lifted_ancestor_z_index_clears_the_scrim():
    """9000 is the scrim; the lift must land strictly above it, and strictly
    below the popover (9020) so the popover still renders on top."""
    css = _tour_css()
    block = css.split(".tour-lifts-ancestor {", 1)[1].split("}", 1)[0]
    z = int(block.split("z-index:", 1)[1].strip().split("!important")[0].strip())
    assert 9000 < z < 9020


def test_scroll_into_view_does_not_scroll_the_page_horizontally():
    """`inline: 'nearest'` (not the default 'center'/'start'/'end') keeps a
    rail anchor near the viewport edge from dragging the whole page sideways
    when the tour scrolls it into view."""
    js = _js()
    assert "inline: 'nearest'" in js


# --- Rail width containment on mobile ------------------------------------


def test_rail_row_layout_contains_its_own_overflow():
    """The base `.rail` rule sets `overflow: visible` so the Studio hover
    flyout can escape the fixed column's right edge — harmless in column
    layout. In the row (mobile) layout `.rail` is back in normal document
    flow, so that same overflow drags the whole page horizontally."""
    css = _rail_css()
    narrow = css.split("@media (max-width: 1024px)", 1)[1]
    assert "overflow-x: auto" in narrow
    assert "max-width: 100%" in narrow


def test_rail_nav_items_do_not_force_full_row_width_on_mobile():
    """`.rail-i` is `width: 100%` for the column layout (each item spans the
    fixed 240px rail). Left unscoped in the row layout, that same 100%
    resolves against the wrapping flex row, forcing every item onto its own
    full-width line."""
    css = _rail_css()
    narrow = css.split("@media (max-width: 1024px)", 1)[1]
    assert ".rail-i {\n        width: auto;" in narrow
