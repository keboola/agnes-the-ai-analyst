"""Server-rendered HTML must carry `Cache-Control: no-store`.

Regression guard for the stale-`/home` install bug: the setup hero bakes the
current wheel filename into the markup at render time, and that filename is
served from the version-pinned `/cli/wheel/{name}` endpoint which 404s for any
name but the wheel currently on disk. If the browser heuristically caches the
HTML, a redeploy (new wheel on disk) leaves the user with a stale page whose
baked wheel URL now 404s. The middleware sets `no-store` on text/html so every
load re-renders against the live build.
"""

from fastapi.testclient import TestClient


def test_html_page_carries_no_store():
    from app.main import app
    client = TestClient(app)
    # /login is an unauthenticated HTML page (renders the provider form).
    resp = client.get("/login")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert resp.headers.get("cache-control") == "no-store"


def test_json_api_is_not_marked_no_store():
    from app.main import app
    client = TestClient(app)
    # /api/version is JSON (application/json) — the no-store rule is text/html
    # only, so it must not pick up the directive.
    resp = client.get("/api/version")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.headers.get("cache-control") != "no-store"
