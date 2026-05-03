"""POST /api/admin/bigquery/test-connection — admin-only health probe.

Lets an admin verify the saved data_source.bigquery config is reachable
WITHOUT having to hit /api/query or /api/v2/scan and read the failure
mode out of an analyst's error report. Closes #160 §4.9 (the operator-side
half of the USER_PROJECT_DENIED loop the reporter hit).

Cases: admin + reachable BQ → 200; admin + not_configured → 400; admin
+ cross_project_forbidden → 502; admin + 10s timeout → 504; non-admin →
403; unauthenticated → 401.
"""
from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_test_connection_success(seeded_app, monkeypatch):
    """A reachable BQ project returns 200 with resolved projects + elapsed_ms."""
    class _FakeJob:
        job_id = "fake-1"
        location = "US"

        def result(self, timeout=None):
            return [{"ok": 1}]

    class _FakeClient:
        def query(self, sql):
            assert "SELECT 1" in sql.upper()
            return _FakeJob()

    class _FakeProjects:
        billing = "prj-billing"
        data = "prj-data"

    class _FakeBqAccess:
        projects = _FakeProjects()

        def client(self):
            return _FakeClient()

    monkeypatch.setattr(
        "app.api.admin_bigquery_test.get_bq_access",
        lambda: _FakeBqAccess(),
        raising=False,
    )

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post("/api/admin/bigquery/test-connection", headers=_auth(token))
    assert r.status_code == 200, r.json()
    body = r.json()
    assert body.get("ok") is True
    assert body.get("billing_project") == "prj-billing"
    assert body.get("data_project") == "prj-data"
    assert isinstance(body.get("elapsed_ms"), (int, float))


def test_test_connection_not_configured(seeded_app, monkeypatch):
    """When BQ isn't configured (no project), return 400 with the typed
    not_configured detail surface so the admin sees a clear next step."""
    from connectors.bigquery.access import BqAccessError

    def fake_get_bq_access():
        raise BqAccessError(
            "not_configured",
            "BigQuery project not configured",
            details={"hint": "Set data_source.bigquery.project in instance.yaml"},
        )

    monkeypatch.setattr(
        "app.api.admin_bigquery_test.get_bq_access",
        fake_get_bq_access,
        raising=False,
    )

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post("/api/admin/bigquery/test-connection", headers=_auth(token))
    assert r.status_code == 400, r.json()
    detail = r.json().get("detail", {})
    if isinstance(detail, dict):
        assert detail.get("kind") == "not_configured"


def test_test_connection_cross_project_forbidden(seeded_app, monkeypatch):
    """USER_PROJECT_DENIED translates to 502 with cross_project_forbidden
    detail — same shape as /api/v2/scan returns, so the CLI renderer
    surfaces it identically across both paths."""
    from connectors.bigquery.access import BqAccessError

    class _FakeProjects:
        billing = ""
        data = "prj-data"

    class _FakeBqAccess:
        projects = _FakeProjects()

        def client(self):
            raise BqAccessError(
                "cross_project_forbidden",
                "USER_PROJECT_DENIED on bigquery.googleapis.com",
                details={
                    "billing_project": "",
                    "data_project": "prj-data",
                    "hint": "Set data_source.bigquery.billing_project",
                },
            )

    monkeypatch.setattr(
        "app.api.admin_bigquery_test.get_bq_access",
        lambda: _FakeBqAccess(),
        raising=False,
    )

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post("/api/admin/bigquery/test-connection", headers=_auth(token))
    assert r.status_code == 502, r.json()
    detail = r.json().get("detail", {})
    if isinstance(detail, dict):
        assert detail.get("kind") == "cross_project_forbidden"


def test_test_connection_timeout(seeded_app, monkeypatch):
    """A query that hangs past the 10s polling timeout returns 504. Best-
    effort cancel_job is called; surface caveat that the BQ job may keep
    running until BQ side-cancels it."""
    import concurrent.futures as _cf

    class _FakeJob:
        job_id = "slow-1"
        location = "US"

        def result(self, timeout=None):
            raise _cf.TimeoutError()

    class _FakeClient:
        def query(self, sql):
            return _FakeJob()

        def cancel_job(self, job_id, project=None, location=None):
            pass  # best-effort no-op

    class _FakeProjects:
        billing = "prj-billing"
        data = "prj-data"

    class _FakeBqAccess:
        projects = _FakeProjects()

        def client(self):
            return _FakeClient()

    monkeypatch.setattr(
        "app.api.admin_bigquery_test.get_bq_access",
        lambda: _FakeBqAccess(),
        raising=False,
    )

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post("/api/admin/bigquery/test-connection", headers=_auth(token))
    assert r.status_code == 504, r.json()
    detail = r.json().get("detail", {})
    if isinstance(detail, dict):
        assert detail.get("kind") == "timeout"


def test_test_connection_non_admin_403(seeded_app):
    """Non-admin users cannot probe BQ from the admin UI."""
    c = seeded_app["client"]
    analyst_token = seeded_app["analyst_token"]
    r = c.post("/api/admin/bigquery/test-connection", headers=_auth(analyst_token))
    assert r.status_code == 403, r.json()


def test_test_connection_unauthenticated_401(seeded_app):
    """Unauthenticated requests get 401."""
    c = seeded_app["client"]
    r = c.post("/api/admin/bigquery/test-connection")
    assert r.status_code == 401, r.json()
