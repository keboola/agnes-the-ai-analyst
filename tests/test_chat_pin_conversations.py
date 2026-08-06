"""Static-source guards for the conversation row menu (Pin · Rename · Delete).

Two renderers draw the same history list — chat.js on /chat, rail_history.js
everywhere else (see the header comment in rail_history.js). The row actions live
in ONE shared component, js/components/chat_row_menu.js, so the guards here are
mostly about the seams that would silently break if only one side changed:

  - both renderers delegate to ``window.chatRowMenu`` (no hand-rolled buttons);
  - both stamp ``data-pinned="1"`` and hoist a "Pinned" group;
  - the panel's CSS is GLOBAL, because chat.css is loaded only by /chat while the
    panel is body-appended and used on every rail page;
  - both host templates load the component script.

No headless browser in CI — we assert the source contract the way
test_chat_surface_badge.py does.
"""

import re
from pathlib import Path

CHAT_JS = Path("app/web/static/js/chat.js")
RAIL_JS = Path("app/web/static/js/rail_history.js")
MENU_JS = Path("app/web/static/js/components/chat_row_menu.js")
CHAT_CSS = Path("app/web/static/css/chat.css")
RAIL_CSS = Path("app/web/static/css/rail.css")
MENU_CSS = Path("app/web/static/css/chat_row_menu.css")
# The reference dropdown — the detail-page overflow menu this panel must match.
DETAIL_CSS = Path("app/web/static/css/detail-page.css")
BASE_DS = Path("app/web/templates/base_ds.html")
CHAT_HTML = Path("app/web/templates/chat.html")
RAIL_HTML = Path("app/web/templates/_app_rail.html")

_RENDERERS = (CHAT_JS, RAIL_JS)


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# --- The shared component owns the actions ------------------------------


def test_both_renderers_delegate_to_the_shared_row_menu():
    """Neither renderer may hand-roll the menu: one component is what keeps the
    action set identical on /chat and off it."""
    for path in _RENDERERS:
        js = _read(path)
        assert "window.chatRowMenu" in js, f"{path} must use the shared row menu"
        assert "chatRowMenu.trigger(" in js, f"{path} must build its trigger via the component"


def test_renderers_no_longer_carry_inline_row_action_buttons():
    """Regression guard: the pin toggle and the delete "×" used to sit inline in
    the row. They moved INTO the menu — a stray one would mean two ways to do the
    same thing, and in a ~250px rail there is no width for it."""
    for path in _RENDERERS:
        js = _read(path)
        assert "cloud-chat-list-pin" not in js, f"{path} still emits the old inline pin toggle"
        assert "cloud-chat-list-del" not in js, f"{path} still emits the old inline delete button"


def test_menu_offers_pin_rename_delete_with_archive_optional_between():
    """Pin · Rename · Delete is the base set, in that order.

    Archive (or Restore, on an archived row) is spliced in BETWEEN Rename and
    Delete — after the harmless actions, before the irreversible one — and only
    where the caller passes the handler. The two rail renderers don't: neither
    lists archived conversations, so archiving from there would put a chat
    somewhere the caller cannot see it again. /chats does (chats_page.js), which
    is the surface that can list and undo the state."""
    js = _read(MENU_JS)
    ids = re.findall(r'id:\s*"([a-z]+)"', js)
    assert ids == ["pin", "rename", "restore", "archive", "delete"], f"unexpected action set: {ids}"
    assert '"Unpin" : "Pin"' in js, "the pin item must flip its verb on a pinned row"
    assert '"Rename"' in js
    assert '"Delete"' in js
    # Archive and Restore are mutually exclusive per row, chosen from its state.
    assert "s.archived && opts.onRestore" in js
    assert "!s.archived && opts.onArchive" in js
    # Delete is the only destructive one and must be marked as such — Archive
    # explicitly is not: it is reversible, and dressing the two alike is how a
    # caller learns to fear the safe one (or trust the other).
    assert re.search(r'label:\s*"Delete",\s*danger:\s*true', js)
    assert not re.search(r'label:\s*"Archive",[^}]*danger', js)


def test_menu_is_a_single_global_and_runs_only_once():
    """A rail /chat page loads the component from BOTH hosts, so a second
    execution must not re-register the document-level listeners (every keystroke
    would otherwise be handled twice)."""
    js = _read(MENU_JS)
    assert "if (window.chatRowMenu) return;" in js
    assert "window.chatRowMenu = {" in js


def test_menu_is_keyboard_operable_and_dismissible():
    js = _read(MENU_JS)
    for key in ('"Escape"', '"ArrowDown"', '"ArrowUp"'):
        assert key in js, f"row menu must handle {key}"
    assert "aria-haspopup" in js and "aria-expanded" in js
    assert 'setAttribute("role", "menu")' in js
    assert 'setAttribute("role", "menuitem")' in js
    # The modifier guard this used to require went with the letter accelerators
    # it protected (Cmd-R had to keep reloading); see
    # test_menu_advertises_no_single_letter_shortcuts.


def test_menu_click_does_not_also_open_the_conversation():
    """The trigger sits inside a row that is itself a button — without
    stopPropagation, opening the menu would also navigate/open the session."""
    js = _read(MENU_JS)
    assert "stopPropagation" in js


def test_renderers_close_the_menu_before_re_rendering():
    """The panel is body-appended, so a menu left open across a re-render would
    hover over a row that no longer exists."""
    for path in _RENDERERS:
        js = _read(path)
        assert "chatRowMenu.close()" in js, f"{path} must close the menu before wiping the list"


# --- Pin state ---------------------------------------------------------


def test_both_renderers_stamp_data_pinned_and_render_a_pin_flag():
    for path in _RENDERERS:
        js = _read(path)
        assert re.search(r'dataset\.pinned\s*=\s*"1"', js), f'{path} must stamp dataset.pinned = "1"'
        # With the pin ACTION behind the menu, a pinned row needs a visible mark
        # of its own — the group header scrolls away.
        assert "cloud-chat-pin-flag" in js, f"{path} must mark a pinned row"


def test_both_renderers_separate_the_pinned_conversations():
    """A pinned conversation is never left inline among the dated ones — but
    WHERE it goes now depends on the chrome, so each renderer is checked against
    the contract it actually implements:

      - rail: pinned rows are rendered into the Pinned SECTION's own list
        (<ul id="pinned-chat-list">, _app_rail.html) — a collapsible section
        with its own header, so no group header is emitted for them.
      - topnav: unchanged, pinned rows are hoisted into a leading "Pinned"
        group inside the single #chat-list.

    chat.js serves both, hence both strings; rail_history.js only ever runs
    under rail."""
    for path in _RENDERERS:
        js = _read(path)
        assert "pinned-chat-list" in js, f"{path} must render pinned rows into the Pinned section"
    chat_js = _read(CHAT_JS)
    assert '"Pinned"' in chat_js, "topnav must still label its hoisted group"
    assert "pinnedGroup" in chat_js, "topnav must still flag that group for the header class"
    # ...and the rail path must not go through the date grouping at all, or a
    # pinned chat would appear in both the section and the feed. It renders a
    # capped, ungrouped Recent feed of the UNPINNED rows instead — which is what
    # retired the `hoistPinned` opt-out this used to assert.
    assert "s => !s.pinned" in chat_js, "the rail feed must exclude the pinned rows"
    assert "hoistPinned" not in chat_js, "the grouping helper is topnav-only now — no rail opt-out to pass"
    assert "pinnedGroup" not in _read(RAIL_JS), "the rail renderer has no pinned GROUP any more"


def test_both_renderers_call_the_pin_endpoint_with_put():
    for path in _RENDERERS:
        js = _read(path)
        assert "/pin" in js and '"PUT"' in js, f"{path} must PUT the pin endpoint"


# --- A pin can never be hidden ------------------------------------------


def test_the_recent_cap_never_reaches_the_pins():
    """The rail's recent feed is capped again (RAIL_RECENT_LIMIT), now that
    "View all chats" → /chats gives the cap a destination — but the cap applies
    to the DATED feed only. Pinned rows live in their own uncapped section, and
    hiding one would break the single promise pinning makes.

    This is not the old apparatus coming back: no truncation pass, no
    MutationObserver, no persisted expanded-state, and no per-row `hidden` —
    the renderer simply slices the list it draws. Both absences are asserted so
    the machinery cannot return under cover of the cap."""
    js = _read(RAIL_JS)
    assert "li.hidden" not in js, "no per-row hiding — the cap is a slice, not a truncation pass"
    assert "new MutationObserver" not in js, "the truncation observer must stay retired"
    # The cap is applied to the DATED feed and to nothing else; the pinned list
    # is filled from its own unsliced collection.
    assert "for (const s of dated.slice(0, RAIL_RECENT_LIMIT))" in js
    assert "for (const s of pinned) pinnedList.appendChild" in js, "pins must never be capped"


# --- Rename / Delete flows ---------------------------------------------


def test_rename_uses_the_app_modal_and_the_title_endpoint():
    for path in _RENDERERS:
        js = _read(path)
        assert "promptModal" in js, f"{path} must use the app-wide promptModal for rename"
        assert "/title" in js, f"{path} must PUT the rename endpoint"
        # promptModal resolves null on cancel — an empty title must not be sent.
        assert "next === null" in js, f"{path} must treat a cancelled prompt as a no-op"


def test_delete_is_confirmed_in_both_renderers():
    """Delete is one keystroke ("D") away from Pin inside the menu, so it cannot
    be unconfirmed."""
    for path in _RENDERERS:
        js = _read(path)
        assert "confirmModal" in js, f"{path} must confirm before deleting"
        assert "danger: true" in js, f"{path}'s delete confirmation must be styled destructive"


# --- CSS / loading contract -------------------------------------------


def test_panel_css_is_global_not_chat_only():
    """chat.css is loaded ONLY by chat.html, but the panel is body-appended and
    used on every rail page — so its rules must live in the globally loaded
    sheet, or the menu renders unstyled everywhere except /chat."""
    assert MENU_CSS.exists(), "the row-menu panel needs its own globally loaded sheet"
    css = _read(MENU_CSS)
    assert ".chat-rowmenu {" in css and ".chat-rowmenu__item" in css
    assert "css/chat_row_menu.css" in _read(BASE_DS), "base_ds.html must load the panel sheet"
    # And it must not have been left behind in the /chat-only sheet.
    assert ".chat-rowmenu__item" not in _read(CHAT_CSS)


def test_panel_wears_the_house_dropdown_look():
    """There is ONE dropdown look in the app, and this panel is not exempt.

    The detail-page overflow menu, the Library move menu and the filter menu all
    agree on four numbers — 6px panel padding on a 12px radius with
    --ds-shadow-md, and 13.5px rows on an 8px radius. The row menu used to
    follow the --radius-*/--text-* size ladder instead (6px/4px/12px), which is
    what made it read as a different component. Asserted against
    detail-page.css so drift in EITHER sheet fails here rather than shipping as
    two menus that don't match.
    """
    for path, panel_sel, item_sel in (
        (MENU_CSS, r"\.chat-rowmenu \{", r"\.chat-rowmenu__item \{"),
        (DETAIL_CSS, r"\.detail-menu__panel \{", r"\.detail-menu__item \{"),
    ):
        css = _read(path)
        panel = re.search(panel_sel + r"([^}]*)\}", css)
        item = re.search(item_sel + r"([^}]*)\}", css)
        assert panel and item, f"{path} must declare the menu panel + its rows"
        assert "padding: 6px" in panel.group(1), f"{path}: panel padding"
        assert "border-radius: 12px" in panel.group(1), f"{path}: panel radius"
        assert "box-shadow: var(--ds-shadow-md)" in panel.group(1), f"{path}: panel elevation"
        assert "font-size: 13.5px" in item.group(1), f"{path}: row type size"
        assert "border-radius: 8px" in item.group(1), f"{path}: row radius"


def test_every_action_carries_a_glyph():
    """The house menus label each row with a muted leading icon; a menu of bare
    labels reads as a different component even at matching metrics. The glyph is
    keyed off the action's own id, so an action can't be added without one."""
    js = _read(MENU_JS)
    for name in ("pin", "rename", "delete"):
        assert f"{name}:" in js, f"ITEM_SVG must define the {name} glyph"
    assert "ITEM_SVG[action.id]" in js, "the glyph must follow the action id"
    assert ".chat-rowmenu__item svg" in _read(MENU_CSS), "the glyph needs a size + muted ink"


def test_menu_advertises_no_single_letter_shortcuts():
    """The panel used to print P / R / D beside its rows and bind them off
    `event.key` — the character the LAYOUT produces, so on a non-Latin layout
    the same physical keys emit other characters and nothing fired. Hints and
    bindings were removed together; a hint with no working binding (or a binding
    with no hint) is the state to guard against."""
    js = _read(MENU_JS)
    assert "chat-rowmenu__key" not in js, "no accelerator hint may be rendered"
    assert "chat-rowmenu__key" not in _read(MENU_CSS), "dead accelerator styles must go too"
    assert not re.search(r"\bkey:\s*\"[a-z]\"", js), "no action may declare a letter binding"
    # Layout-independent keyboard support stays.
    for k in ("Escape", "ArrowDown", "ArrowUp", "Home", "End"):
        assert f'"{k}"' in js, f"{k} handling must survive"


def test_trigger_is_styled_in_both_hosts():
    """The trigger lives inside the row, so each host sizes its own."""
    for path in (CHAT_CSS, RAIL_CSS):
        assert ".chat-rowmenu-btn" in _read(path), f"{path} must style the row trigger"


def test_row_menu_css_uses_only_ds_tokens():
    for path in (MENU_CSS, CHAT_CSS, RAIL_CSS):
        css = _read(path)
        for block in re.findall(r"[^{}]*\.chat-rowmenu[^{}]*\{([^}]*)\}", css):
            assert not re.search(r"#[0-9a-fA-F]{3,6}\b", block), f"no raw hex allowed in {path}"
            assert not re.search(r"var\(\s*--primary[-)\s,]", block), (
                f"use var(--ds-primary…), not legacy var(--primary…) in {path}"
            )


def test_pinned_flag_is_not_signalled_by_color_alone():
    """The flag is filled rather than outlined, so pin state survives grayscale
    and color-vision differences."""
    for path in (CHAT_CSS, RAIL_CSS):
        css = _read(path)
        assert re.search(r"\.cloud-chat-pin-flag svg \{[^}]*fill:\s*currentColor", css), (
            f"{path} must fill the pin flag glyph"
        )


def test_trigger_stays_reachable_on_touch_and_while_open():
    """Reveal-on-hover never fires on a touchscreen, and the trigger must not
    look like it vanished while its own menu is open."""
    for path in (CHAT_CSS, RAIL_CSS):
        css = _read(path)
        assert '.chat-rowmenu-btn[aria-expanded="true"]' in css, f"{path} must hold the trigger visible while open"
        hover_none = re.search(r"@media \(hover: none\)\s*\{(.*?)\n\}", css, re.DOTALL)
        assert hover_none, f"{path} needs a coarse-pointer fallback"
        assert "chat-rowmenu-btn" in hover_none.group(1), f"{path} must reveal the trigger on touch"


def test_both_hosts_load_the_component_script():
    for path in (CHAT_HTML, RAIL_HTML):
        assert "js/components/chat_row_menu.js" in _read(path), f"{path} must load the row-menu component"


# --- The dropdown-Escape focus fix this feature depended on -------------


def test_nav_dropdown_escape_does_not_steal_focus_when_closed():
    """app.js wires the user/admin dropdowns with a document-level Escape
    handler. It used to close-and-focus UNCONDITIONALLY, so any Escape anywhere
    on the page pulled focus onto the profile trigger — which silently defeated
    the row menu's own "Escape returns focus to the trigger" (both listeners are
    on `document`, and app.js's runs second).

    Guarded here because the symptom is invisible in a static read of either file
    on its own."""
    js = Path("app/web/static/app.js").read_text(encoding="utf-8")
    m = re.search(r'if \(e\.key !== "Escape"\) return;(.*?)\n        \}\);', js, re.DOTALL)
    assert m, "could not locate the dropdown Escape handler in app.js"
    body = m.group(1)
    assert 'getAttribute("aria-expanded") !== "true"' in body and "return;" in body, (
        "the dropdown Escape handler must bail unless its own panel is open"
    )
