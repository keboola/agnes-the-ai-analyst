"""Tests for Keboola materialized registration."""
import pytest


@pytest.fixture(autouse=True)
def _keboola_instance(monkeypatch):
    """Configure the test instance with a Keboola data source so the new
    register-table source_type-availability validator (introduced in this
    PR) accepts `source_type='keboola'` payloads. Pre-validator the test
    suite passed without any data_source config because the route blindly
    persisted whatever source_type the caller sent."""
    fake_cfg = {
        "data_source": {
            "type": "keboola",
            "keboola": {
                "stack_url": "https://connection.keboola.com",
                "project_id": "1234",
                "token_env": "KEBOOLA_STORAGE_TOKEN",
            },
        },
    }
    monkeypatch.setattr(
        "app.instance_config.load_instance_config", lambda: fake_cfg, raising=False,
    )
    from app.instance_config import reset_cache
    reset_cache()
    yield
    reset_cache()


def test_register_keboola_materialized_accepts_json_filter_spec(seeded_app):
    """Keboola materialized source_query must be a JSON filter spec, not SQL."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    auth = {"Authorization": f"Bearer {token}"}
    r = c.post(
        "/api/admin/register-table",
        headers=auth,
        json={
            "name": "orders_recent",
            "source_type": "keboola",
            "query_mode": "materialized",
            "source_query": '{"columns": ["order_id", "date"], "changedSince": "-7 days"}',
            "sync_schedule": "daily 03:00",
        },
    )
    assert r.status_code == 201, r.text


def test_register_keboola_materialized_accepts_missing_source_query(seeded_app):
    """A NULL source_query on a keboola materialized row means
    'full-table export via Storage API export-async' — no SQL needed.
    The admin path must accept it. (BigQuery materialized has the same
    no-source-query semantic via the SELECT *-from-bucket auto-fill in
    the BQ branch of register_table; for keboola the export-async API
    takes a structured filter, not a SQL string, so we just persist
    NULL and let the extractor pass an empty ExportFilter.)"""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    auth = {"Authorization": f"Bearer {token}"}
    r = c.post(
        "/api/admin/register-table",
        headers=auth,
        json={
            "name": "orders_recent",
            "source_type": "keboola",
            "query_mode": "materialized",
            "bucket": "in.c-sales",
            "source_table": "orders",
            # source_query intentionally omitted.
        },
    )
    assert r.status_code == 201, r.text


def test_register_keboola_materialized_skips_bucket_check(seeded_app):
    """Materialized rows don't need bucket/source_table. Mirror of BQ materialized validator behavior."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    auth = {"Authorization": f"Bearer {token}"}
    r = c.post(
        "/api/admin/register-table",
        headers=auth,
        json={
            "name": "x",
            "source_type": "keboola",
            "query_mode": "materialized",
            # No bucket / source_table / source_query — full-table export.
        },
    )
    assert r.status_code == 201, r.text


def test_update_keboola_materialized_clears_stale_source_query_on_mode_switch(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    auth = {"Authorization": f"Bearer {token}"}

    # Register materialized (no source_query = full-table export).
    r = c.post(
        "/api/admin/register-table",
        headers=auth,
        json={
            "name": "x",
            "source_type": "keboola",
            "query_mode": "materialized",
        },
    )
    assert r.status_code == 201

    # PUT to switch back to local — source_query must clear.
    r = c.put(
        "/api/admin/registry/x",
        headers=auth,
        json={
            "source_type": "keboola",
            "query_mode": "local",
            "bucket": "in.c-foo",
            "source_table": "y",
        },
    )
    assert r.status_code == 200

    r = c.get("/api/admin/registry", headers=auth)
    rows = r.json()["tables"]
    row = next(t for t in rows if t["id"] == "x")
    assert row.get("source_query") in (None, "")


def test_update_keboola_to_materialized_without_source_query_allowed(seeded_app):
    """Keboola materialized with null source_query = full-table export; valid.

    The Keboola extractor's materialize_query() explicitly handles null
    source_query as a full-table export (see extractor.py:138 'if source_query:').
    The PUT handler must not require source_query for Keboola materialized rows —
    blocking this would prevent admins from updating any other field on a
    Keboola materialized row that was registered without source_query.

    Devin finding 2026-06-01 (BUG_pr-review-job-f5c4c30af9f647e2ac636b1a48361e65_0001).
    """
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    auth = {"Authorization": f"Bearer {token}"}

    # Register a Keboola local row (source_query intentionally absent).
    r = c.post(
        "/api/admin/register-table",
        headers=auth,
        json={
            "name": "kb_local",
            "source_type": "keboola",
            "bucket": "in.c-foo",
            "source_table": "events",
            "query_mode": "local",
        },
    )
    assert r.status_code == 201, r.text

    # Flip to materialized WITHOUT source_query — valid (full-table export).
    r = c.put(
        "/api/admin/registry/kb_local",
        headers=auth,
        json={"query_mode": "materialized"},
    )
    assert r.status_code == 200, r.text

    # Updating an unrelated field on an already-materialized row with null
    # source_query must also succeed (the merged row keeps source_query=None).
    r = c.put(
        "/api/admin/registry/kb_local",
        headers=auth,
        json={"description": "updated description"},
    )
    assert r.status_code == 200, r.text
