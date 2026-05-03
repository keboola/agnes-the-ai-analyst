"""POST /api/query must reject direct `bigquery_query()` function calls.

This is a pre-existing RBAC bypass: `bigquery_query('proj', 'SELECT * FROM
ds.tbl')` runs a BQ jobs API call against any reachable dataset, ignoring
the master-view forbidden-table check that gates registered names. Closes
that hole by adding `bigquery_query` to the SQL keyword blocklist.

Internal wrap views (created by the BQ extractor) use bigquery_query()
inside their CREATE VIEW body — those run via DuckDB's view resolution at
query time, NOT via user-submitted SQL, so the blocklist doesn't break
them. Closes part of #160.
"""
from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_bigquery_query_function_call_rejected(seeded_app):
    """Plain `SELECT * FROM bigquery_query(...)` is blocked at the
    keyword-blocklist layer with the canonical "Only single SELECT
    queries are allowed" detail."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    sql = "SELECT * FROM bigquery_query('proj', 'SELECT 1 AS x')"
    r = c.post(
        "/api/query",
        json={"sql": sql},
        headers=_auth(token),
    )
    assert r.status_code == 400, f"expected 400; got {r.status_code} body={r.json()}"
    detail = str(r.json().get("detail", ""))
    # The canonical blocklist message proves this was rejected by the
    # blocklist (not by some other path like master-view-forbidden).
    assert "single SELECT" in detail, \
        f"expected canonical blocklist message; got detail={detail!r}"


def test_bigquery_query_mixed_case_rejected(seeded_app):
    """Existing blocklist runs `sql.strip().lower()` first, so any case
    variant is blocked uniformly."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/query",
        json={"sql": "SELECT * FROM BigQuery_Query('proj', 'SELECT 1')"},
        headers=_auth(token),
    )
    assert r.status_code == 400, r.json()
    detail = str(r.json().get("detail", ""))
    assert "single SELECT" in detail, \
        f"expected canonical blocklist message; got detail={detail!r}"


def test_bigquery_query_with_whitespace_before_paren_rejected(seeded_app):
    """Substring match catches `bigquery_query (...)` with space too."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/query",
        json={"sql": "SELECT * FROM bigquery_query   ('proj', 'SELECT 1')"},
        headers=_auth(token),
    )
    assert r.status_code == 400, r.json()
    detail = str(r.json().get("detail", ""))
    assert "single SELECT" in detail, \
        f"expected canonical blocklist message; got detail={detail!r}"
