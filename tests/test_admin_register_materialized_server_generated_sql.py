"""When admin registers a materialized BQ row with bucket+source_table
but NO source_query, the server generates the source_query from the
configured BQ project + the supplied bucket/source_table. Admin never
has to know about bigquery_query() syntax for the trivial full-table
dump case.

Fixtures `seeded_app`, `bq_instance`, `stub_bq_extractor` are auto-
discovered from `tests/conftest.py` — DO NOT import. `seeded_app`
is a dict: `{"client": TestClient, "admin_token": str, ...}`.
"""
from __future__ import annotations

import pytest


def _auth(token: str) -> dict:
    """Mirror the project's local _auth helper used in every materialized
    test file (e.g. test_api_admin_materialized.py)."""
    return {"Authorization": f"Bearer {token}"}


def test_register_materialized_with_bucket_only_generates_source_query(
    seeded_app, bq_instance, stub_bq_extractor,
):
    client = seeded_app["client"]
    headers = _auth(seeded_app["admin_token"])
    payload = {
        "name": "trivial_full_dump",
        "source_type": "bigquery",
        "query_mode": "materialized",
        "bucket": "analytics",
        "source_table": "orders_v2",
    }
    resp = client.post("/api/admin/register-table", json=payload, headers=headers)
    assert resp.status_code in (200, 201, 202), resp.text

    reg = client.get("/api/admin/registry", headers=headers).json()
    row = next(t for t in reg["tables"] if t["id"] == "trivial_full_dump")
    expected_project = bq_instance["data_source"]["bigquery"]["project"]
    assert row["source_query"] == (
        f"SELECT * FROM `{expected_project}.analytics.orders_v2`"
    )


def test_register_materialized_with_explicit_source_query_persists_verbatim(
    seeded_app, bq_instance, stub_bq_extractor,
):
    client = seeded_app["client"]
    headers = _auth(seeded_app["admin_token"])
    custom = "SELECT col1, col2 FROM `analytics.orders_v2` WHERE col3 = 'x'"
    payload = {
        "name": "explicit_sql",
        "source_type": "bigquery",
        "query_mode": "materialized",
        "source_query": custom,
    }
    resp = client.post("/api/admin/register-table", json=payload, headers=headers)
    assert resp.status_code in (200, 201, 202), resp.text

    reg = client.get("/api/admin/registry", headers=headers).json()
    row = next(t for t in reg["tables"] if t["id"] == "explicit_sql")
    assert row["source_query"] == custom


def test_put_flip_to_materialized_with_bucket_generates_source_query(
    seeded_app, bq_instance, stub_bq_extractor,
):
    client = seeded_app["client"]
    headers = _auth(seeded_app["admin_token"])
    # First register as remote.
    client.post(
        "/api/admin/register-table",
        json={"name": "flip_t", "source_type": "bigquery",
              "bucket": "analytics", "source_table": "orders_v2"},
        headers=headers,
    )
    # PUT to flip to materialized without supplying source_query.
    resp = client.put(
        "/api/admin/registry/flip_t",
        json={"query_mode": "materialized"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    reg = client.get("/api/admin/registry", headers=headers).json()
    row = next(t for t in reg["tables"] if t["id"] == "flip_t")
    expected_project = bq_instance["data_source"]["bigquery"]["project"]
    assert row["source_query"] == (
        f"SELECT * FROM `{expected_project}.analytics.orders_v2`"
    )
