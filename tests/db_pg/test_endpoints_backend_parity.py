"""App-level backend-parity integration tests (batch 1).

The repo-layer contract tests pin the data layer; these pin the *endpoint*
layer. Each test seeds state through the backend-aware factory (so the row
lands in whichever backend is active) and then exercises the HTTP endpoint via
``seeded_app_both`` — once on DuckDB, once on real Postgres.

The discriminator: a route that reads system state through the factory returns
the seeded row on BOTH backends; a route that reads through a raw DuckDB
connection returns it on DuckDB but an EMPTY/stale result on Postgres, so the
``[pg]`` parametrization fails — exactly pinpointing a #518/#513 backend-split
bug at the endpoint that has it.

Batch 1: read-list endpoints that are clean pass/fail discriminators.
"""
from __future__ import annotations

import pytest


def _auth(seeded_app_both, who="admin"):
    return {"Authorization": f"Bearer {seeded_app_both[f'{who}_token']}"}


def _as_items(payload):
    """Endpoints return either a bare list or {'items': [...]} / {'metrics': [...]}."""
    if isinstance(payload, list):
        return payload
    for k in ("items", "metrics", "recipes", "tables", "results", "data"):
        if isinstance(payload.get(k), list):
            return payload[k]
    return []


# ---------------------------------------------------------------------------
# GET /api/metrics — seeded via metric_repo()
# ---------------------------------------------------------------------------

def test_metrics_list_reflects_seeded_metric(seeded_app_both):
    from src.repositories import metric_repo
    metric_repo().create(
        id="revenue/parity_probe",
        name="parity_probe",
        display_name="Parity Probe",
        category="revenue",
        sql="SELECT 1",
    )
    r = seeded_app_both["client"].get("/api/metrics", headers=_auth(seeded_app_both))
    assert r.status_code == 200, r.text
    ids = {m.get("id") or m.get("name") for m in _as_items(r.json())}
    assert "revenue/parity_probe" in ids or "parity_probe" in ids, (
        f"[{seeded_app_both['backend']}] seeded metric missing from /api/metrics: {r.json()}"
    )


# ---------------------------------------------------------------------------
# GET /api/recipes — seeded via recipes_repo()
# ---------------------------------------------------------------------------

def test_recipes_list_reflects_seeded_recipe(seeded_app_both):
    from src.repositories import recipes_repo
    recipes_repo().create(
        slug="parity-probe",
        title="Parity Probe",
        description="probe",
        icon=None,
        color=None,
        sql_template="SELECT 1",
        related_table_ids=None,
        created_by="admin1",
    )
    r = seeded_app_both["client"].get("/api/recipes", headers=_auth(seeded_app_both))
    assert r.status_code == 200, r.text
    slugs = {x.get("slug") or x.get("title") for x in _as_items(r.json())}
    assert "parity-probe" in slugs or "Parity Probe" in slugs, (
        f"[{seeded_app_both['backend']}] seeded recipe missing from /api/recipes: {r.json()}"
    )


# ---------------------------------------------------------------------------
# GET /api/v2/catalog — seeded via table_registry_repo()
# ---------------------------------------------------------------------------

def test_v2_catalog_reflects_seeded_table(seeded_app_both):
    from src.repositories import table_registry_repo
    table_registry_repo().register(
        id="parity_probe_tbl",
        name="Parity Probe Table",
        query_mode="local",
        source_type="keboola",
        bucket="in.c-main",
        description="probe",
    )
    r = seeded_app_both["client"].get("/api/v2/catalog", headers=_auth(seeded_app_both))
    assert r.status_code == 200, r.text
    ids = {t.get("id") or t.get("name") for t in _as_items(r.json())}
    assert "parity_probe_tbl" in ids or "Parity Probe Table" in ids, (
        f"[{seeded_app_both['backend']}] seeded table missing from /api/v2/catalog: {r.json()}"
    )


# ---------------------------------------------------------------------------
# Web catalog DETAIL pages (app/web/router.py) — these read table_registry via
# a raw DuckDB conn (grandfathered residual), so on Postgres get() returns None
# → HTTP 404 for a table that exists. Seeded via the factory.
# ---------------------------------------------------------------------------

def test_web_catalog_table_detail_renders_seeded_table(seeded_app_both):
    from src.repositories import table_registry_repo
    table_registry_repo().register(
        id="probe_detail_tbl",
        name="Probe Detail Table",
        query_mode="local",
        source_type="keboola",
        bucket="in.c-main",
        description="probe",
    )
    r = seeded_app_both["client"].get(
        "/catalog/t/probe_detail_tbl", headers=_auth(seeded_app_both)
    )
    assert r.status_code == 200, (
        f"[{seeded_app_both['backend']}] /catalog/t/{{id}} returned {r.status_code} "
        f"for a table seeded through the factory — the route reads table_registry "
        f"off a raw DuckDB conn instead of table_registry_repo()."
    )
