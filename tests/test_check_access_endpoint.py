"""Unit tests for ``GET /api/data/{table_id}/check-access`` — the
lightweight RBAC probe used by Caddy's ``forward_auth`` directive to gate
file_server-served parquet downloads without involving the app's request
workers in the bulk byte transfer.

The endpoint must:
  - return 204 when the caller has read access (admin → always; non-admin
    only with an explicit ``resource_grants`` row),
  - return 403 with no body / minimal body when the caller does not,
  - return 404 for unsafe identifiers (path-traversal guard),
  - return 401 when the request has no auth.
"""

from tests.conftest import create_mock_extract


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_admin_gets_204(seeded_app):
    """Admin short-circuits all RBAC checks — must always succeed."""
    c = seeded_app["client"]
    env = seeded_app["env"]
    create_mock_extract(env["extracts_dir"], "keboola", [
        {"name": "salaries", "data": [{"id": "1"}]},
    ])
    from src.orchestrator import SyncOrchestrator
    SyncOrchestrator().rebuild()
    c.post(
        "/api/admin/register-table",
        json={"name": "salaries", "source_type": "keboola"},
        headers=_auth(seeded_app["admin_token"]),
    )

    resp = c.get(
        "/api/data/salaries/check-access",
        headers=_auth(seeded_app["admin_token"]),
    )
    assert resp.status_code == 204
    assert resp.content == b""


def test_analyst_without_grant_gets_403(seeded_app):
    """Non-admin without an explicit `resource_grants` row must be denied
    — the production failure mode where Caddy's forward_auth returns the
    403 to the client and never invokes file_server."""
    c = seeded_app["client"]
    env = seeded_app["env"]
    create_mock_extract(env["extracts_dir"], "keboola", [
        {"name": "salaries", "data": [{"id": "1"}]},
    ])
    from src.orchestrator import SyncOrchestrator
    SyncOrchestrator().rebuild()
    c.post(
        "/api/admin/register-table",
        json={"name": "salaries", "source_type": "keboola"},
        headers=_auth(seeded_app["admin_token"]),
    )

    resp = c.get(
        "/api/data/salaries/check-access",
        headers=_auth(seeded_app["analyst_token"]),
    )
    assert resp.status_code == 403


def test_analyst_with_grant_gets_204(seeded_app):
    """Once the analyst has a TABLE grant, check-access flips to 204
    and Caddy is free to serve the file directly. Mirrors the same
    grant flow used by ``/api/data/{id}/download``."""
    c = seeded_app["client"]
    env = seeded_app["env"]
    create_mock_extract(env["extracts_dir"], "keboola", [
        {"name": "salaries", "data": [{"id": "1"}]},
    ])
    from src.orchestrator import SyncOrchestrator
    SyncOrchestrator().rebuild()
    c.post(
        "/api/admin/register-table",
        json={"name": "salaries", "source_type": "keboola"},
        headers=_auth(seeded_app["admin_token"]),
    )

    # Mint the grant via the admin API the same way the existing download
    # access-control tests do — see test_access_control.py.
    from tests.test_access_control import _grant_table_to_analyst
    from src.db import get_system_db
    conn = get_system_db()
    try:
        _grant_table_to_analyst(conn, "salaries")
    finally:
        conn.close()

    resp = c.get(
        "/api/data/salaries/check-access",
        headers=_auth(seeded_app["analyst_token"]),
    )
    assert resp.status_code == 204


def test_unsafe_table_id_gets_404(seeded_app):
    """Identifier validation runs BEFORE RBAC — keeps path-traversal
    payloads (``../etc/passwd``) from reaching ``can_access_table`` and
    matches the pre-existing behavior of ``/download``."""
    c = seeded_app["client"]
    resp = c.get(
        "/api/data/..%2Fetc%2Fpasswd/check-access",
        headers=_auth(seeded_app["admin_token"]),
    )
    # FastAPI's path converter rejects encoded slashes outright; either
    # 404 from the validator or 404 from no-such-route is acceptable —
    # both block the traversal. The point is no 5xx and no 204.
    assert resp.status_code in (404, 422)


def test_no_auth_gets_401(seeded_app):
    """Caddy will only call the auth-check endpoint when the client sent
    credentials — but if a request slips through without them, the
    endpoint must reject with 401 so Caddy returns 401 to the client
    instead of falling through to file_server with no identity."""
    c = seeded_app["client"]
    resp = c.get("/api/data/salaries/check-access")
    assert resp.status_code == 401
