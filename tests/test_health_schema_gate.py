"""Health-check schema-version check is opt-in (#204) and severity (#178).

Two adjacent behaviors:

- `GET /api/health/detailed` no longer includes `db_schema` by default.
  Pass `?include=schema` to get it. Rationale: the schema version is
  rarely actionable on a healthy instance and used to dominate the
  agent-facing `agnes diagnose` output.

- `info` severity entries appear in the response but never promote the
  overall status to `degraded` (only `warning` does) or `unhealthy`
  (only `error` does). This lets the BQ billing-equals-data check stay
  visible without falsely tripping the headline.

The `bq_config == info` assertion is in test_diagnose_billing.py; here
we cover the schema gate and a synthetic `info`-doesn't-promote case.
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_schema_check_omitted_by_default(seeded_app):
    """Default response does not include `db_schema` (issue #204)."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/api/health/detailed", headers=_auth(token))
    assert r.status_code == 200, r.text
    assert "db_schema" not in r.json().get("services", {})


def test_schema_check_present_when_include_schema(seeded_app):
    """`?include=schema` returns the legacy entry verbatim."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get(
        "/api/health/detailed",
        headers=_auth(token),
        params={"include": "schema"},
    )
    assert r.status_code == 200, r.text
    services = r.json().get("services", {})
    assert "db_schema" in services
    # Healthy seeded test app must report ok against the current schema.
    assert services["db_schema"].get("db_schema") == "ok", services["db_schema"]


def test_unrecognised_include_token_is_ignored(seeded_app):
    """Unknown include tokens don't error or surface; forward-compatible."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get(
        "/api/health/detailed",
        headers=_auth(token),
        params={"include": "schema,bogus"},
    )
    assert r.status_code == 200, r.text
    services = r.json().get("services", {})
    assert "db_schema" in services
    assert "bogus" not in services


def test_info_severity_does_not_promote_overall(seeded_app, monkeypatch):
    """Issue #178: a service returning `status: info` must NOT push the
    headline to `degraded`. Only `warning`+ does that.

    We synthesize an `info` entry by patching `_check_bq_billing_project`
    (any of the lazy checks would do) so we exercise the aggregator
    without depending on a particular check's natural state. (Previously this
    used `_check_session_pipeline`, disabled on js/new-scheduling.)
    """
    import app.api.health as health_mod

    def _fake_bq_config():
        return {"status": "info", "detail": "synthetic info entry"}

    monkeypatch.setattr(
        health_mod, "_check_bq_billing_project", _fake_bq_config
    )

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/api/health/detailed", headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["services"]["bq_config"]["status"] == "info"
    # The critical assertion — info must not promote the headline.
    assert body["status"] == "healthy", body["status"]
