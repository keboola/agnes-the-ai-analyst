"""Static-source guards for the cross-surface badge + deep-link auto-open
in chat.js / chat.css. No headless browser in CI — we assert the source
contract the way test_design_system_contract.py guards templates/CSS."""
from pathlib import Path

CHAT_JS = Path("app/web/static/js/chat.js")
CHAT_CSS = Path("app/web/static/css/chat.css")


def _js() -> str:
    return CHAT_JS.read_text(encoding="utf-8")


# --- Deep-link one-shot auto-open ---------------------------------------

def test_js_reads_initial_session_from_body_hook():
    js = _js()
    # Reads the DOM hook emitted by chat.html's body_attrs block.
    assert "dataset.initialSession" in js


def test_js_deep_link_is_one_shot_and_guarded():
    js = _js()
    # Guarded so a later sidebar refresh can't re-hijack the view, and
    # consumed exactly once (set to null after use).
    assert "_initialSessionId" in js
    assert "!currentChatId" in js
    assert "requestAnimationFrame" in js


def test_js_deep_link_open_helper_defined():
    js = _js()
    assert "_maybeOpenInitialSession" in js


# --- Surface badge (Slack pill) -----------------------------------------

def test_js_badge_class_emitted_in_make_sidebar_item():
    js = _js()
    assert "cloud-chat-surface-badge" in js
    # Both Slack surfaces trigger the pill; web does not.
    assert "slack_dm" in js
    assert "slack_thread" in js


def test_js_badge_text_is_slack_not_icon():
    js = _js()
    # Text label, not a brand asset (design-system contract: no bundled
    # Slack icon). The pill's textContent is the literal "Slack".
    assert '"Slack"' in js or "'Slack'" in js


def test_chat_css_has_surface_badge_rule():
    css = CHAT_CSS.read_text(encoding="utf-8")
    assert ".cloud-chat-surface-badge" in css


def test_chat_css_surface_badge_uses_only_ds_tokens():
    """The badge rule must reference design tokens (var(--ds-*)) and contain
    NO raw #hex literal and NO legacy var(--primary). Mirrors the design-
    system contract that test_design_system_contract.py enforces on
    templates — applied here to the new chat.css rule block."""
    import re

    css = CHAT_CSS.read_text(encoding="utf-8")
    # Isolate the badge rule block: from the selector to its closing brace.
    m = re.search(r"\.cloud-chat-surface-badge\s*\{(.*?)\}", css, re.DOTALL)
    assert m, "could not locate .cloud-chat-surface-badge rule block"
    block = m.group(1)
    assert "var(--ds-" in block, "badge must use --ds-* design tokens"
    assert not re.search(r"#[0-9a-fA-F]{3,6}\b", block), "no raw hex allowed"
    assert not re.search(r"var\(\s*--primary[-)\s,]", block), (
        "use var(--ds-primary…), not legacy var(--primary…)"
    )
