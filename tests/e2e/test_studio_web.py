"""Browser E2E for the authoring-agent studio pages (all four domains).

Deterministic: each domain's Create action calls its real admin endpoint
(data-packages / mcp-sources / marketplaces / memory-domains), so the assertion
never depends on the LLM. Runs against the docker-compose stack with
LOCAL_DEV_MODE (auto-admin) + fake-agent mode. Records a video per domain to
tests/e2e/_videos/.

Gated (see conftest + docker-compose.e2e.yml):
    AGNES_E2E=1            # boot the stack
    AGNES_E2E_DEV_MODE=1   # bypass auth, seed admin dev user
    AGNES_E2E_FAKE_AGENT=1 # deterministic runner (no Anthropic)
    ANTHROPIC_API_KEY=...  # any non-empty value (compose requires it)
Plus Playwright + Chromium (pip install -e '.[dev]'; playwright install chromium).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

try:
    from playwright.sync_api import Error as _PwErr
    from playwright.sync_api import sync_playwright as _spw

    _PW = True
except ImportError:  # pragma: no cover
    _spw = None  # type: ignore[assignment]
    _PwErr = Exception  # type: ignore[assignment,misc]
    _PW = False

_VIDEO_DIR = Path(__file__).parent / "_videos"

# (domain, {text-field: value}, {select-field: value})
CASES = [
    ("data-package", {"name": "E2E DP", "slug": "e2e-dp", "description": "e2e"}, {}),
    ("corporate-memory", {"name": "E2E Mem", "slug": "e2e-mem", "description": "e2e"}, {}),
    ("mcp", {"name": "e2e_mcp", "url": "https://mcp.example.com/sse"}, {"transport": "http"}),
    (
        "marketplace",
        {
            "name": "E2E MP",
            "slug": "e2e-mp",
            "url": "https://github.com/example/repo",
            "curator_name": "E2E",
            "curator_email": "e2e@example.com",
        },
        {},
    ),
]


@pytest.fixture
def video_ctx(docker_e2e_agnes):
    if not _PW:
        pytest.skip("playwright not installed — pip install -e '.[dev]'")
    if not os.environ.get("AGNES_E2E"):
        pytest.skip("E2E disabled — set AGNES_E2E=1")
    if not os.environ.get("AGNES_E2E_DEV_MODE"):
        pytest.skip("studio E2E needs admin — set AGNES_E2E_DEV_MODE=1")
    _VIDEO_DIR.mkdir(exist_ok=True)
    pw = _spw().start()
    try:
        browser = pw.chromium.launch()
    except _PwErr as exc:
        pw.stop()
        pytest.skip(f"chromium not installed — playwright install chromium ({exc})")
    yield browser, docker_e2e_agnes
    browser.close()
    pw.stop()


@pytest.mark.parametrize("domain,texts,selects", CASES, ids=[c[0] for c in CASES])
def test_builder_creates_entity(video_ctx, domain, texts, selects):
    browser, base = video_ctx
    ctx = browser.new_context(
        record_video_dir=str(_VIDEO_DIR),
        record_video_size={"width": 1280, "height": 800},
        viewport={"width": 1280, "height": 800},
    )
    page = ctx.new_page()
    try:
        # LOCAL_DEV_MODE auto-logs in as admin, so we land straight on the page.
        page.goto(f"{base}/admin/studio/{domain}", wait_until="domcontentloaded")
        page.wait_for_selector("#studio-create", timeout=15_000)
        for key, val in texts.items():
            page.fill(f"#studio-f-{key}", val)
        for key, val in selects.items():
            page.select_option(f"#studio-f-{key}", val)
        page.click("#studio-create")
        # The result line shows "Created: …" once the real endpoint returns 201.
        page.wait_for_selector("text=Created:", timeout=15_000)
        assert "Created:" in page.inner_text("#studio-result")
    finally:
        ctx.close()  # finalizes the .webm
