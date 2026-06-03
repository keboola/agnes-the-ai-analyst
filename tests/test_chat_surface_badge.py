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
