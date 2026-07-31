"""Static-source guards for the rail's onboarding ("Finish setup") popover
close behavior
(#1037) and the journey panel head / mobile nav collapse (#1039).

No headless browser in CI — these assert the source contract the way
test_chat_surface_badge.py and test_design_system_contract.py do.

Three defects:

1. "×", click-away, and Escape all correctly manipulate `.is-open` in JS, but
   `.rail-getstarted-panel` is ALSO revealed by CSS `:hover` / `:focus-within`
   — which outrank the removal of `.is-open` by specificity, so closing had
   no visible effect while the cursor was still over the launcher (exactly
   the moment a close action fires) or while the toggle button itself held
   focus (it's a descendant, so `:focus-within` stays true).
2. `.cloud-chat-journey-head` was a non-wrapping row; once the "Complete ✓"
   badge appeared it overflowed, and the panel's `overflow-y: auto` clipped
   the ↻ / × buttons past the edge. They were also 20x20px, under the 44px
   touch-target minimum.
3. Below 1024px the rail becomes a wrapping top bar with no way to collapse
   it — the nav zones + recents + Admin stayed permanently on screen.
"""

import re
from pathlib import Path

RAIL_CSS = Path("app/web/static/css/rail.css")
CHAT_CSS = Path("app/web/static/css/chat.css")
RAIL_HISTORY_JS = Path("app/web/static/js/rail_history.js")
ONBOARDING_JS = Path("app/web/static/js/chat_onboarding.js")
RAIL_TEMPLATE = Path("app/web/templates/_app_rail.html")


def _rail_css() -> str:
    return RAIL_CSS.read_text(encoding="utf-8")


def _chat_css() -> str:
    return CHAT_CSS.read_text(encoding="utf-8")


def _rail_history_js() -> str:
    return RAIL_HISTORY_JS.read_text(encoding="utf-8")


def _onboarding_js() -> str:
    return ONBOARDING_JS.read_text(encoding="utf-8")


def _strip_js_comments(js: str) -> str:
    """Drop `//` and `/* */` comments so an assertion can target CODE only.

    Needed by any guard phrased as "this identifier must not appear": the file
    that removed a thing usually names it in the comment explaining why, and a
    guard that forbids that forces the code to be undocumented. Not a full
    tokenizer — it does not know about `//` inside a string literal — which is
    fine for the rail scripts and would only ever over-strip, never under-strip.
    """
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.DOTALL)
    return re.sub(r"^\s*//.*$", "", js, flags=re.MULTILINE)


def _rail_template() -> str:
    return RAIL_TEMPLATE.read_text(encoding="utf-8")


# --- #1037: Get started popover close --------------------------------------


def test_is_closed_override_beats_hover_and_focus_within():
    css = _rail_css()
    assert ".rail-getstarted.is-closed .rail-getstarted-panel" in css
    block = css.split(".rail-getstarted.is-closed .rail-getstarted-panel {", 1)[1].split("}", 1)[0]
    assert "display: none !important" in block


def test_close_paths_set_is_closed():
    """Every JS path that closes the panel (toggle click-to-close, outside
    click, Escape, and the "×" in chat_onboarding.js) must add `.is-closed`,
    not just remove `.is-open` — removing `.is-open` alone is what the hover
    rule silently defeated."""
    rail_js = _rail_history_js()
    assert 'gsWrap.classList.toggle("is-closed", !open)' in rail_js

    onboarding_js = _onboarding_js()
    assert 'wrap.classList.add("is-closed")' in onboarding_js


def test_mouseleave_lifts_the_close_suppression():
    """`.is-closed` must not permanently disable hover-to-preview — only
    suppress the same hover session that was just explicitly closed."""
    js = _rail_history_js()
    assert 'gsWrap.addEventListener("mouseleave"' in js
    assert 'classList.remove("is-closed")' in js


# --- #1039: journey head overflow + touch targets --------------------------


def test_journey_head_wraps_instead_of_overflowing():
    for css, selector in (
        (_rail_css(), 'html[data-ui-layout="rail"] .cloud-chat-journey-head {'),
        (_chat_css(), ".cloud-chat-journey-head {"),
    ):
        block = css.split(selector, 1)[1].split("}", 1)[0]
        assert "flex-wrap: wrap" in block


def test_journey_head_title_truncates_instead_of_forcing_overflow():
    for css, selector in (
        (_rail_css(), 'html[data-ui-layout="rail"] .cloud-chat-journey-head h3 {'),
        (_chat_css(), ".cloud-chat-journey-head h3 {"),
    ):
        block = css.split(selector, 1)[1].split("}", 1)[0]
        assert "min-width: 0" in block
        assert "text-overflow: ellipsis" in block


def test_journey_iconbtn_hit_area_meets_touch_target_minimum():
    """The visual pill stays 20px; the actual hit area is expanded via an
    invisible ::before rather than growing the element (which would look
    oversized in this compact header)."""
    for css, selector in (
        (_rail_css(), 'html[data-ui-layout="rail"] .cloud-chat-journey-iconbtn::before {'),
        (_chat_css(), ".cloud-chat-journey-iconbtn::before {"),
    ):
        block = css.split(selector, 1)[1].split("}", 1)[0]
        assert "inset: -12px" in block  # 20px visual + 12px*2 = 44px hit area


# --- Merged #1040: rail cannot collapse below 1024px -------------------------


def test_rail_has_a_collapse_toggle():
    html = _rail_template()
    assert 'id="rail-collapse-toggle"' in html
    assert 'aria-controls="rail-collapsible"' in html


def test_collapsible_wrapper_spans_nav_history_and_admin_only():
    """The wrapper must open right after the logo/toggle and close before
    .rail-foot — Finish setup + the user menu must stay reachable regardless
    of the collapsed state."""
    html = _rail_template()
    assert html.index('id="rail-collapsible"') < html.index('class="rail-nav rail-nav-top"')
    assert html.index('class="rail-admin"') < html.index("/.rail-collapsible")
    assert html.index("/.rail-collapsible") < html.index('class="rail-foot"')


def test_collapse_toggle_is_inert_above_the_breakpoint():
    """Hidden by default (unscoped) so the column layout is unaffected;
    only shown inside the ≤1024px media query."""
    css = _rail_css()
    unscoped = css.split("@media (max-width: 1024px)", 1)[0]
    scoped = css.split("@media (max-width: 1024px)", 1)[1]
    base_block = unscoped.split('html[data-ui-layout="rail"] .rail-collapse-toggle {', 1)[1].split("}", 1)[0]
    assert "display: none" in base_block
    assert 'html[data-ui-layout="rail"] .rail-collapse-toggle {' in scoped


def test_collapsible_wrapper_is_transparent_above_the_breakpoint():
    """Nesting nav/history/admin one level deeper must not change the
    default (unscoped) column layout — this needs to reproduce .rail's own
    flex/gap/growth exactly, or spacing and the Chats-list scroll region
    both shift for every existing topnav-unaffected, rail-enabled instance."""
    css = _rail_css()
    unscoped = css.split("@media (max-width: 1024px)", 1)[0]
    block = unscoped.split('html[data-ui-layout="rail"] .rail-collapsible {', 1)[1].split("}", 1)[0]
    assert "display: flex" in block
    assert "flex-direction: column" in block
    assert "gap: 2px" in block
    assert "flex: 1 1 auto" in block
    assert "min-height: 0" in block
    # Must NOT set overflow — the Studio hover flyout (rail-nav,
    # position:absolute) needs to keep escaping through this wrapper exactly
    # as it did through .rail directly.
    assert "overflow" not in block


def test_javascript_toggles_nav_open_state():
    js = _rail_history_js()
    assert 'getElementById("rail-collapse-toggle")' in js
    assert 'classList.toggle("is-nav-open"' in js


# --- "Start over onboarding" (profile menu) ---------------------------------


def test_restart_onboarding_is_wired_from_both_boot_paths():
    """The rail mounts the journey module two ways — chat.js's
    initChatOnboarding on /chat, and mountJourneyPanel everywhere else. The
    profile-menu entry must be wired in BOTH, or restarting works on one half of
    the app. Wired before the awaited fetch so the click works as soon as the
    page is interactive."""
    js = _onboarding_js()
    assert js.count("wireRestartOnboardingMenuItem()") == 3  # 1 definition + 2 calls
    init = js.split("export async function initChatOnboarding", 1)[1].split("}", 1)[0]
    assert "wireRestartOnboardingMenuItem()" in init
    assert init.index("wireRestartOnboardingMenuItem()") < init.index("await loadJourney()")
    mount = js.split("export async function mountJourneyPanel", 1)[1].split("\n}", 1)[0]
    assert "wireRestartOnboardingMenuItem()" in mount


def test_restart_clears_every_step_and_the_soft_dismiss():
    """Clearing the steps is what un-retires the Finish setup row (rail.css
    hides it on `.is-complete`). `dismissed` has to go too — an earlier "×" this
    page load would otherwise swallow the checklist the caller just restarted."""
    js = _onboarding_js()
    body = js.split("function restartJourney() {", 1)[1].split("\n}", 1)[0]
    assert "dismissed = false" in body
    for key in (
        "onboarded: false",
        "first_asked: false",
        "stack_setup_done: false",
        "explored_stack: false",
        "catalog_discovered: false",
        "use_anywhere: false",
    ):
        assert key in body, f"restart must clear {key}"
    # The checklist's own "Start over" button shares the same function.
    assert 'restartBtn.addEventListener("click", restartJourney)' in js


def test_restart_from_the_menu_closes_it_and_reveals_the_row():
    """Restarting from a menu needs feedback the checklist's own button doesn't:
    close the profile menu and pin the Finish setup popover, so the row the
    caller was promised is actually on screen. `.is-closed` must be lifted —
    it's the only thing that beats rail.css's own hover/focus reveal."""
    js = _onboarding_js()
    body = js.split("function wireRestartOnboardingMenuItem() {", 1)[1].split("\n}\n", 1)[0]
    assert 'setAttribute("hidden", "")' in body  # profile menu closed
    assert 'classList.remove("is-closed")' in body
    assert 'classList.add("is-open")' in body


def test_history_list_is_resolved_at_its_use_site():
    """Regression guard, from a real breakage: the rail rendered NO conversations
    on any page except /chat.

    `#chat-list` used to be looked up once at the top of the IIFE, as a
    module-level `listEl` sitting beside the recent-list truncation state. When
    the truncation was removed the whole declaration block went with it, but the
    line that consumed it (`const list = listEl;`) stayed — so the script threw
    `ReferenceError: listEl is not defined` before it ever fetched, and the
    conversation list came up empty everywhere `rail_history.js` owns it. Which
    looked exactly like "clicking Library makes my chats disappear".

    `node --check` cannot catch this: it is valid syntax that fails at runtime.
    So the guard is that the element is resolved WHERE it is used, and that no
    identifier from the deleted block is referenced anywhere.
    """
    js = _rail_history_js()
    assert 'const list = document.getElementById("chat-list");' in js
    # Every name that lived in the deleted truncation block. Any reference to one
    # of these is a dangling read of state that no longer exists.
    #
    # Checked against CODE only — the comment above the fix names these very
    # identifiers to explain the breakage, and a guard that forbids a file from
    # documenting its own history is a guard that gets deleted.
    code = _strip_js_comments(js)
    for orphan in ("listEl", "toggleTxt", "applyTruncation", "RECENT_LIMIT", "EXPANDED_KEY"):
        assert orphan not in code, f"{orphan} was deleted with the truncation block — nothing may reference it"
