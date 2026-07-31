"""Static-source guards for the coach-mark tour popover's positioning and
responsive layout.

The tour engine is pure client-side (no headless browser in CI), so these
assert the source contract the way test_chat_surface_badge.py and
test_design_system_contract.py do.

Three defects, one root cause each:

1. Positioning read the anchor's PRE-scroll bounding rect because
   `requestAnimationFrame` (~16ms) fired long before a `behavior: 'smooth'`
   `scrollIntoView` (~300-500ms) had settled — the popover landed wherever the
   anchor used to be and was never corrected.
2. There was no vertical viewport clamp (only horizontal), so a card taller
   than the space on either side of its anchor got pinned to the very top of
   the screen and spilled past the fold with nothing to scroll it.
3. `tour.css` had no width breakpoints at all — every dimension was a desktop
   constant, and the final step's button stacking applied unconditionally at
   every width despite a comment claiming otherwise.
"""

from pathlib import Path

TOUR_JS = Path("app/web/static/js/tour.js")
TOUR_CSS = Path("app/web/static/css/tour.css")


def _js() -> str:
    return TOUR_JS.read_text(encoding="utf-8")


def _css() -> str:
    return TOUR_CSS.read_text(encoding="utf-8")


# --- Stale-rect positioning ------------------------------------------------


def test_scroll_into_view_no_longer_positions_on_the_next_frame_only():
    """The old bug: scrollIntoView(smooth) + a single requestAnimationFrame.
    Positioning must instead wait for the anchor's rect to stop moving."""
    js = _js()
    assert "_waitForScrollSettle" in js
    assert "getBoundingClientRect()" in js
    # A settle timeout must exist so a scroll that never stabilizes can't hang
    # the tour forever.
    assert "SCROLL_SETTLE_TIMEOUT_MS" in js


def test_scroll_into_view_does_not_scroll_the_page_horizontally():
    """`inline: 'nearest'` (not 'center'/'start'/'end') keeps a mobile rail
    from being dragged sideways when its anchor is scrolled into view."""
    js = _js()
    assert "inline: 'nearest'" in js


# --- Vertical containment ----------------------------------------------


def test_popover_has_a_vertical_viewport_clamp():
    """CSS counterpart to the horizontal `calc(100vw - 24px)` clamp — without
    it a tall card has nothing stopping it from spilling past the fold."""
    css = _css()
    assert "max-height: calc(100dvh - 24px)" in css


def test_popover_body_scrolls_independently_of_header_and_footer():
    css = _css()
    body_block = css.split(".tour-popover-body {", 1)[1].split("}", 1)[0]
    assert "overflow-y: auto" in body_block


def test_a_card_that_fits_neither_side_takes_the_roomier_one_and_caps_itself():
    """Centering used to be the fallback whenever the card fit on neither side
    of its anchor — which lays it straight OVER the thing the step points at.
    Fatal for the composer step, where the anchor is what the user is being
    asked to type into. The card now takes the side with more room and caps
    itself to it (the body scrolls, so nothing is unreachable)."""
    js = _js()
    assert "spaceBelow" in js and "spaceAbove" in js
    assert "Math.max(spaceBelow, spaceAbove)" in js
    assert "popover.style.maxHeight = `${roomier}px`" in js
    # And the cap comes off before the next measurement, or a reflow into a
    # roomier spot would keep the old constrained height forever.
    assert "popover.style.maxHeight = '';" in js


def test_centering_survives_as_the_last_resort_only():
    """When not even a readable card fits beside the anchor, centering is still
    the honest layout — pinning to the viewport top reads as broken."""
    js = _js()
    assert "MIN_POPOVER_HEIGHT" in js
    assert "roomier < MIN_POPOVER_HEIGHT" in js
    assert "_positionPopover(popover, anchor, true)" in js


# --- Compact breakpoint --------------------------------------------------


def test_tour_css_has_a_width_breakpoint():
    """tour.css previously had zero max-width media queries (only
    prefers-reduced-motion)."""
    css = _css()
    assert "@media (max-width: 600px)" in css


def test_final_step_stacking_is_scoped_to_the_compact_breakpoint():
    """Regression guard: `.tour-popover--final .tour-actions { flex-direction:
    column }` used to apply unconditionally, so the final step's buttons
    stacked full-width even on a 1440px screen."""
    css = _css()
    unscoped = css.split("@media (max-width: 600px)", 1)[0]
    assert ".tour-popover--final .tour-actions {" not in unscoped, (
        "final-step button stacking must live inside the compact media query, not apply at every width"
    )


def test_regular_steps_get_an_explicit_narrow_layout():
    """Below the breakpoint, the three regular-step buttons ("I'll explore on
    my own" / Back / Next) get a deliberate full-width column instead of
    wrapping one-per-line by accident."""
    css = _css()
    compact = css.split("@media (max-width: 600px)", 1)[1]
    assert ".tour-actions,\n  .tour-popover--final .tour-actions {" in compact
    assert "min-height: 44px" in compact
