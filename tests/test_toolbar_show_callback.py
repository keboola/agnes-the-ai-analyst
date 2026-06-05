"""_toolbar_show_callback gates which requests the debug toolbar attaches to.

Document navigations AND data XHRs (e.g. /api/marketplace/items) are
instrumented so their — often async/XHR-loaded — queries show in the panels.
Only the toolbar's own /_debug_toolbar endpoints (always allowed) and a small
set of high-frequency background pollers (_TOOLBAR_SKIP_PREFIXES) are
special-cased; the pollers are skipped because each instrumented response
repoints the toolbar (dtRefresh) and wipes the panel you're viewing.
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


def test_data_xhr_instrumented():
    # The whole point: data-loading XHRs must be captured so their PG queries
    # (marketplace/flea listings, store entities) appear in the panel.
    assert _toolbar_show_callback(_Req("/api/marketplace/items", "empty"), None) is True
    assert _toolbar_show_callback(_Req("/api/store/entities", "empty"), None) is True


def test_toolbar_own_endpoints_always_allowed():
    assert _toolbar_show_callback(_Req("/_debug_toolbar", "empty"), None) is True


def test_background_pollers_skipped():
    for p in ("/api/version", "/api/health", "/api/memory/stats", "/api/notifications"):
        assert _toolbar_show_callback(_Req(p, "empty"), None) is False, p


def test_disabled_when_debug_off(monkeypatch):
    monkeypatch.delenv("DEBUG", raising=False)
    monkeypatch.delenv("LOCAL_DEV_MODE", raising=False)
    assert _toolbar_show_callback(_Req("/dashboard", "document"), None) is False
