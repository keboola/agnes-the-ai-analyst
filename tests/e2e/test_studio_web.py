"""Browser E2E for the data-package builder studio page (authoring Slice 0).

Deterministic: the Create action calls the real /api/admin/data-packages
endpoint, so the assertion never depends on the LLM. Runs against the
docker-compose stack with LOCAL_DEV_MODE (auto-admin) + fake-agent mode.
Records video of the run to tests/e2e/_videos/.

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


@pytest.fixture
def video_page(docker_e2e_agnes):
    if not _PW:
        pytest.skip("playwright not installed — pip install -e '.[dev]'")
    if not os.environ.get("AGNES_E2E"):
        pytest.skip("E2E disabled — set AGNES_E2E=1")
    if not os.environ.get("AGNES_E2E_DEV_MODE"):
        pytest.skip("studio E2E needs admin — set AGNES_E2E_DEV_MODE=1 (LOCAL_DEV_MODE in the stack)")
    _VIDEO_DIR.mkdir(exist_ok=True)
    pw = _spw().start()
    try:
        browser = pw.chromium.launch()
    except _PwErr as exc:
        pw.stop()
        pytest.skip(f"chromium not installed — playwright install chromium ({exc})")
    ctx = browser.new_context(
        record_video_dir=str(_VIDEO_DIR),
        record_video_size={"width": 1280, "height": 800},
        viewport={"width": 1280, "height": 800},
    )
    page = ctx.new_page()
    try:
        yield page, docker_e2e_agnes
    finally:
        ctx.close()  # finalizes the .webm
        browser.close()
        pw.stop()


def test_builder_creates_data_package(video_page):
    page, base = video_page
    # LOCAL_DEV_MODE auto-logs in as an admin, so we land straight on the page.
    page.goto(f"{base}/admin/studio/data-package", wait_until="domcontentloaded")
    page.wait_for_selector("#studio-create", timeout=15_000)
    page.fill("#dp-name", "E2E Finance")
    page.fill("#dp-slug", "e2e-finance")
    page.fill("#dp-description", "Created by the studio E2E")
    page.click("#studio-create")
    # The result line shows "Created: …" on success.
    page.wait_for_selector("text=Created:", timeout=15_000)

    # Verify via the API that the package now exists.
    resp = page.request.get(f"{base}/api/admin/data-packages")
    assert resp.ok, resp.status
    slugs = [p.get("slug") for p in resp.json()]
    assert "e2e-finance" in slugs, slugs
