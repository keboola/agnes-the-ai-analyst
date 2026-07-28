"""GET /api/admin/server-config exposes `placeholder_from` for fields
whose UI placeholder should resolve to another config value at render
time. Used by `data_source.bigquery.billing_project` to surface its
fallback to `data_source.bigquery.project` (see
connectors/bigquery/access.py:339-340).

Closes part of #160 §4.7.5.
"""
from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_billing_project_field_carries_placeholder_from(seeded_app):
    """The known-fields registry must mark billing_project's
    placeholder_from path so the JS template can resolve and inject
    `(defaults to <project>)` at render time."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/api/admin/server-config", headers=_auth(token))
    assert r.status_code == 200, r.json()
    fields = r.json()["known_fields"]["data_source"]["bigquery"]["fields"]
    assert "billing_project" in fields
    spec = fields["billing_project"]
    assert spec.get("placeholder_from") == [
        "data_source", "bigquery", "project",
    ], f"expected placeholder_from path; got {spec.get('placeholder_from')!r}"
