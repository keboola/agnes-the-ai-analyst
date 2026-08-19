"""Server-rendered HTML must carry `Cache-Control: no-store`.

Regression guard for the stale-`/home` install bug: the setup hero bakes
render-time values into the markup — RBAC-filtered plugin grants, the live
connector manifest, the operator's instance brand/host. (The install prompt's
CLI step used to also bake a version-pinned `/cli/wheel/{name}` URL that
404s the moment the server upgrades between render and execution; it now
downloads via the unversioned `/cli/download` endpoint instead, immune to
that race.) If the browser heuristically caches the HTML, a redeploy leaves
the user with a stale page. The middleware sets `no-store` on text/html so
every load re-renders against the live build.
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
