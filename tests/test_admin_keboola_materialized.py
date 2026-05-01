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


def test_register_keboola_materialized_accepts_source_query(seeded_app):
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
            "source_query": "SELECT * FROM kbc.\"in.c-sales\".\"orders\" WHERE date > '2026-01-01'",
            "sync_schedule": "daily 03:00",
        },
    )
    assert r.status_code == 201, r.text


def test_register_keboola_materialized_rejects_missing_source_query(seeded_app):
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
            # source_query missing
        },
    )
    assert r.status_code == 422
    assert "source_query" in r.text


def test_register_keboola_materialized_skips_bucket_check(seeded_app):
    """Materialized rows don't need bucket/source_table — the SELECT inlines
    the references. Mirror of BQ materialized validator behavior."""
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
            "source_query": "SELECT 1",
            # No bucket / source_table — must still succeed.
        },
    )
    assert r.status_code == 201, r.text


def test_update_keboola_materialized_clears_stale_source_query_on_mode_switch(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    auth = {"Authorization": f"Bearer {token}"}

    # Register materialized.
    r = c.post(
        "/api/admin/register-table",
        headers=auth,
        json={
            "name": "x",
            "source_type": "keboola",
            "query_mode": "materialized",
            "source_query": "SELECT 1",
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


def test_update_keboola_to_materialized_without_source_query_rejected(seeded_app):
    """Devin finding 2026-05-01 (BUG_pr-review-job-58ae3148_0001):
    PUT cannot persist a non-BQ materialized row without source_query.
    Pre-fix, the validation only fired for source_type='bigquery' via the
    synthetic RegisterTableRequest; Keboola rows could be flipped to
    materialized with source_query=None and crash at the next sync tick."""
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

    # Try to flip to materialized WITHOUT shipping source_query.
    r = c.put(
        "/api/admin/registry/kb_local",
        headers=auth,
        json={"query_mode": "materialized"},
    )
    assert r.status_code == 422, r.text
    body = r.json()
    detail = body.get("detail", "")
    if isinstance(detail, list):
        detail = " ".join(str(d) for d in detail)
    assert "source_query" in detail.lower(), body
