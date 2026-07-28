"""Playwright browser harness for the cloud-chat web UI.

These tests run a headless Chromium against the docker-compose env
brought up by `docker_e2e_agnes` (see conftest.py). They are gated by
two layers of opt-in:

  * `AGNES_E2E=1` — required to build/boot the docker-compose stack.
    Without it `docker_e2e_agnes` skips, which cascades into every test
    here.
  * Playwright + a Chromium binary must be installed. Install with:

        pip install -e ".[dev]"
        playwright install chromium --with-deps

    If either is missing, the per-test fixtures detect it and skip.

Tests marked `real_llm` require an additional `AGNES_E2E_ANTHROPIC=1`
opt-in (handled by the collection hook in conftest.py) so the default
E2E run stays cheap and deterministic.
"""

from __future__ import annotations

import os

import pytest


try:
    from playwright.sync_api import sync_playwright as _sync_playwright
    from playwright.sync_api import Error as _PlaywrightError

    _PLAYWRIGHT_AVAILABLE = True
except ImportError:  # pragma: no cover — covered by skip below
    _sync_playwright = None  # type: ignore[assignment]
    _PlaywrightError = Exception  # type: ignore[assignment,misc]
    _PLAYWRIGHT_AVAILABLE = False


@pytest.fixture(scope="module")
def chrome():
    """Launch a single headless Chromium for the whole module.

    Module-scoped because browser launch is the expensive part (~1 s on
    a warm machine, ~5 s cold). Per-test isolation is provided by the
    `page` fixture below which opens a fresh `BrowserContext`.
    """
    if not _PLAYWRIGHT_AVAILABLE:
        pytest.skip("playwright not installed — pip install -e '.[dev]'")
    if not os.environ.get("AGNES_E2E"):
        pytest.skip("E2E disabled — set AGNES_E2E=1")
    try:
        pw = _sync_playwright().start()
        try:
            browser = pw.chromium.launch()
        except _PlaywrightError as exc:
            pw.stop()
            pytest.skip(
                f"chromium not installed — run `playwright install chromium "
                f"--with-deps` ({exc})"
            )
        yield browser
        browser.close()
        pw.stop()
    except _PlaywrightError as exc:  # pragma: no cover
        pytest.skip(f"playwright start failed: {exc}")


@pytest.fixture
def page(chrome, docker_e2e_agnes):
    """Fresh browser context + page per test.

    Depends on `docker_e2e_agnes` so the page navigation has something
    to talk to. Each context is isolated (separate cookie jar, storage,
    etc.) so tests don't share auth state.
    """
    ctx = chrome.new_context()
    p = ctx.new_page()
    yield p
    ctx.close()


def test_chat_loads_without_console_errors(page, docker_e2e_agnes):
    """Smoke: /chat renders without JS errors.

    Verifies the bundled vendor assets (marked.min.js, highlight.min.js,
    highlight.min.css) load and parse — the most common breakage when
    the chat UI is deployed for the first time.
    """
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    # We don't assert on a successful chat round-trip here — that's
    # F.1's job. We only need the page to render and the JS bundle to
    # execute without throwing.
    page.goto(f"{docker_e2e_agnes}/chat", wait_until="domcontentloaded")
    page.wait_for_load_state("networkidle")
    assert errors == [], f"console errors on /chat: {errors}"
