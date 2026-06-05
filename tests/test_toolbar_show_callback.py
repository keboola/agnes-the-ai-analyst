"""_toolbar_show_callback gates which requests own the debug toolbar.

Background fetch/XHR (incl. the dashboard's /api polling) must NOT be
instrumented, or each one rewrites the dtRefresh cookie and refresh.js wipes the
request-scoped query panels (DuckDB/Postgres) the developer is viewing. Only
document navigations and the toolbar's own /_debug_toolbar endpoints qualify.
"""
import pytest

from app.main import _toolbar_show_callback


class _Req:
    def __init__(self, path, dest=None):
        self.url = type("U", (), {"path": path})()
        self.headers = {"sec-fetch-dest": dest} if dest else {}


@pytest.fixture(autouse=True)
def _debug_on(monkeypatch):
    monkeypatch.setenv("DEBUG", "1")


def test_document_navigation_instrumented():
    assert _toolbar_show_callback(_Req("/dashboard", "document"), None) is True


def test_non_browser_request_instrumented():
    # curl / tests send no Sec-Fetch-Dest — keep the toolbar working there.
    assert _toolbar_show_callback(_Req("/dashboard", None), None) is True


def test_toolbar_own_endpoints_always_allowed():
    # render_panel + static are XHR; must stay allowed or panels never load.
    assert _toolbar_show_callback(_Req("/_debug_toolbar", "empty"), None) is True


def test_background_xhr_skipped():
    assert _toolbar_show_callback(_Req("/dashboard", "empty"), None) is False


def test_api_poll_skipped():
    assert _toolbar_show_callback(_Req("/api/memory/stats", "empty"), None) is False
    assert _toolbar_show_callback(_Req("/api/anything", "document"), None) is False


def test_disabled_when_debug_off(monkeypatch):
    monkeypatch.delenv("DEBUG", raising=False)
    monkeypatch.delenv("LOCAL_DEV_MODE", raising=False)
    assert _toolbar_show_callback(_Req("/dashboard", "document"), None) is False
