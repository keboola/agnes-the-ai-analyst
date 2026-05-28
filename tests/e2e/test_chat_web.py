# tests/e2e/test_chat_web.py
import os
import pytest

try:
    from playwright.sync_api import sync_playwright as _sync_playwright
    _PLAYWRIGHT_AVAILABLE = True
except ImportError:
    _PLAYWRIGHT_AVAILABLE = False


@pytest.mark.skipif(
    not os.environ.get("AGNES_E2E") or not _PLAYWRIGHT_AVAILABLE,
    reason="E2E disabled (set AGNES_E2E=1 and install playwright)",
)
def test_chat_e2e_send_and_receive():
    with _sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        # Login flow uses existing helper at /test-login fixture endpoint
        page.goto("http://localhost:8000/test-login?email=e2e@x")
        page.goto("http://localhost:8000/chat")
        page.click("#new-chat")
        page.fill("#chat-input", "hello")
        page.click("#chat-form button[type=submit]")
        page.wait_for_selector(".msg-assistant", timeout=15000)
        text = page.text_content(".msg-assistant")
        assert "hello" in text.lower() or "echo" in text.lower()
        browser.close()
