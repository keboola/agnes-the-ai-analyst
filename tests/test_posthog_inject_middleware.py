"""Regression tests for ``app/middleware/posthog_inject.py``.

Two narrow concerns from PR #231 review (minasarustamyan):

1. ``Response.background`` MUST be forwarded on every return path.
   ``BaseHTTPMiddleware`` materialises the body and asks subclasses to
   return a fresh ``Response``; a missed ``background`` parameter cancels
   any ``BackgroundTask`` / ``BackgroundTasks`` the route attached, with
   no log line.
2. Oversized HTML responses must short-circuit gracefully — the
   middleware buffers in memory by design, so a streamed-HTML route
   would blow up RSS without a cap.

Tests boot a minimal FastAPI app (no DB, no auth, no real PostHog) and
run via ``TestClient`` so they exercise the actual middleware stack.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
from starlette.background import BackgroundTask


@pytest.fixture
def posthog_enabled(monkeypatch):
    monkeypatch.setenv("POSTHOG_API_KEY", "phc_test")
    from src.observability import reset_posthog
    reset_posthog()
    yield
    reset_posthog()


def _make_app() -> FastAPI:
    """Minimal FastAPI app with the injection middleware mounted.

    Avoids importing ``app.main`` so the test stays fast and self-contained.
    """
    from app.middleware.posthog_inject import PosthogInjectionMiddleware

    app = FastAPI()
    app.add_middleware(PosthogInjectionMiddleware)
    return app


def test_background_task_runs_on_html_response(posthog_enabled):
    """A BackgroundTask attached to an HTMLResponse must still fire after
    the middleware rewrites the body. Was silently dropped before fix."""
    fired: list[bool] = []

    def _mark():
        fired.append(True)

    with patch("posthog.Posthog"):
        # ``_render_snippet`` reaches into app.web.router; stub it so the
        # middleware doesn't drag in the full app dependency tree.
        with patch("app.middleware.posthog_inject._render_snippet", return_value="<!--ph-->"):
            app = _make_app()

            @app.get("/page", response_class=HTMLResponse)
            def page():
                return HTMLResponse(
                    "<html><head></head><body>x</body></html>",
                    background=BackgroundTask(_mark),
                )

            client = TestClient(app)
            res = client.get("/page")

    assert res.status_code == 200
    assert "<!--ph-->" in res.text  # snippet injected
    # Background task ran. Without the fix, fired stays [].
    assert fired == [True]


def test_background_task_runs_when_snippet_render_fails(posthog_enabled):
    """If snippet rendering raises, the response still serves and the
    background task still fires."""
    fired: list[bool] = []

    def _mark():
        fired.append(True)

    with patch("posthog.Posthog"):
        with patch(
            "app.middleware.posthog_inject._render_snippet",
            side_effect=RuntimeError("template blew up"),
        ):
            app = _make_app()

            @app.get("/page", response_class=HTMLResponse)
            def page():
                return HTMLResponse(
                    "<html><head></head><body>x</body></html>",
                    background=BackgroundTask(_mark),
                )

            client = TestClient(app)
            res = client.get("/page")

    assert res.status_code == 200
    assert fired == [True]


def test_background_task_runs_when_snippet_already_present(posthog_enabled):
    """Defensive double-injection guard path — body unchanged but
    background still forwarded."""
    fired: list[bool] = []

    def _mark():
        fired.append(True)

    with patch("posthog.Posthog"):
        with patch("app.middleware.posthog_inject._render_snippet", return_value="<!--ph-->"):
            app = _make_app()

            @app.get("/page", response_class=HTMLResponse)
            def page():
                # Body already contains posthog.init -> middleware skips re-injection.
                return HTMLResponse(
                    "<html><head><script>posthog.init('x')</script></head><body></body></html>",
                    background=BackgroundTask(_mark),
                )

            client = TestClient(app)
            res = client.get("/page")

    assert res.status_code == 200
    assert fired == [True]


def test_non_html_response_passthrough_does_not_buffer(posthog_enabled):
    """JSON / non-HTML responses must skip the middleware entirely —
    no body materialisation, no background-task interference."""
    fired: list[bool] = []

    def _mark():
        fired.append(True)

    with patch("posthog.Posthog"):
        app = _make_app()

        @app.get("/api/health")
        def health():
            from fastapi.responses import JSONResponse
            return JSONResponse({"ok": True}, background=BackgroundTask(_mark))

        client = TestClient(app)
        res = client.get("/api/health")

    assert res.status_code == 200
    assert res.json() == {"ok": True}
    assert fired == [True]


def test_oversized_html_response_short_circuits(posthog_enabled, monkeypatch):
    """Body bigger than the buffer cap serves without injection rather
    than buffering arbitrarily large streams in memory."""
    monkeypatch.setattr("app.middleware.posthog_inject._MAX_BUFFER_BYTES", 1024)

    with patch("posthog.Posthog"):
        with patch("app.middleware.posthog_inject._render_snippet", return_value="<!--ph-->"):
            app = _make_app()

            @app.get("/big", response_class=HTMLResponse)
            def big():
                # 2 KB body — twice the patched cap.
                return HTMLResponse("<html><head></head><body>" + ("X" * 2048) + "</body></html>")

            client = TestClient(app)
            res = client.get("/big")

    assert res.status_code == 200
    # Snippet NOT injected — middleware bailed out at the cap.
    assert "<!--ph-->" not in res.text
