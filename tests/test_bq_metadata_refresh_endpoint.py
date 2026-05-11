"""End-to-end tests for the three bq_metadata_refresh endpoints."""

from unittest.mock import patch

from app.api._metadata_models import TableMetadata


def _register_remote(seeded_app, table_id: str):
    from src.db import get_system_db
    from src.repositories.table_registry import TableRegistryRepository
    conn = get_system_db()
    try:
        TableRegistryRepository(conn).register(
            name=table_id,
            id=table_id,
            source_type="bigquery",
            bucket="dwh_base",
            source_table=table_id,
            query_mode="remote",
        )
    finally:
        conn.close()


# ─── POST /api/admin/run-bq-metadata-refresh ──────────────────────────────


def test_run_refresh_walks_remote_rows_and_upserts(seeded_app):
    from src.db import get_system_db
    from src.repositories.bq_metadata_cache import BqMetadataCacheRepository

    _register_remote(seeded_app, "a_remote")
    _register_remote(seeded_app, "b_remote")

    fake = TableMetadata(
        rows=5, size_bytes=512, partition_by="d", clustered_by=["c"],
    )

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    with patch("connectors.bigquery.metadata.fetch", return_value=fake):
        r = c.post(
            "/api/admin/run-bq-metadata-refresh",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] >= 2
    assert body["succeeded"] >= 2
    assert body["failed"] == 0

    conn = get_system_db()
    try:
        repo = BqMetadataCacheRepository(conn)
        for tid in ("a_remote", "b_remote"):
            row = repo.get(tid)
            assert row is not None
            assert row["rows"] == 5
            assert row["size_bytes"] == 512
            assert row["partition_by"] == "d"
            assert row["clustered_by"] == ["c"]
    finally:
        conn.close()


def test_run_refresh_marks_error_on_provider_failure(seeded_app):
    from src.db import get_system_db
    from src.repositories.bq_metadata_cache import BqMetadataCacheRepository

    _register_remote(seeded_app, "boom")

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    with patch(
        "connectors.bigquery.metadata.fetch",
        side_effect=RuntimeError("BQ throttle"),
    ):
        r = c.post(
            "/api/admin/run-bq-metadata-refresh",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["failed"] >= 1

    conn = get_system_db()
    try:
        row = BqMetadataCacheRepository(conn).get("boom")
        assert row is not None
        assert row["error_at"] is not None
        assert "BQ throttle" in (row["error_msg"] or "")
    finally:
        conn.close()


def test_run_refresh_requires_admin(seeded_app):
    c = seeded_app["client"]
    # No Authorization header → 401.
    r = c.post("/api/admin/run-bq-metadata-refresh")
    assert r.status_code == 401


# ─── POST /api/v2/metadata-cache/refresh?table= ───────────────────────────


def test_refresh_one_table_endpoint(seeded_app):
    from src.db import get_system_db
    from src.repositories.bq_metadata_cache import BqMetadataCacheRepository

    _register_remote(seeded_app, "single")

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    fake = TableMetadata(rows=99, size_bytes=999)
    with patch("connectors.bigquery.metadata.fetch", return_value=fake):
        r = c.post(
            "/api/v2/metadata-cache/refresh?table=single",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ok"

    conn = get_system_db()
    try:
        row = BqMetadataCacheRepository(conn).get("single")
        assert row["rows"] == 99
    finally:
        conn.close()


def test_refresh_one_table_unknown_id_returns_404(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/v2/metadata-cache/refresh?table=does_not_exist",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 404


def test_refresh_one_table_rejects_non_remote(seeded_app):
    from src.db import get_system_db
    from src.repositories.table_registry import TableRegistryRepository

    conn = get_system_db()
    try:
        TableRegistryRepository(conn).register(
            name="local_t",
            id="local_t",
            source_type="keboola",
            bucket="in.c-x",
            source_table="t",
            query_mode="local",
        )
    finally:
        conn.close()

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/v2/metadata-cache/refresh?table=local_t",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400


# ─── GET /api/v2/metadata-cache/status ────────────────────────────────────


def test_status_endpoint_returns_per_row_freshness(seeded_app):
    from src.db import get_system_db
    from src.repositories.bq_metadata_cache import BqMetadataCacheRepository

    conn = get_system_db()
    try:
        BqMetadataCacheRepository(conn).upsert_success(
            "orders", rows=1, size_bytes=1, partition_by=None, clustered_by=None,
        )
    finally:
        conn.close()

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get(
        "/api/v2/metadata-cache/status",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "scheduler_interval_seconds" in body
    assert "fresh_threshold_seconds" in body
    assert body["fresh_threshold_seconds"] == 2 * body["scheduler_interval_seconds"]
    orders = next(t for t in body["tables"] if t["table_id"] == "orders")
    assert orders["freshness"] == "fresh"


def test_status_endpoint_does_not_require_admin(seeded_app):
    """Non-admin analyst tools (CLI, Claude Code) need this surface."""
    c = seeded_app["client"]
    # No token at all → 401 (auth still required, just not admin).
    r = c.get("/api/v2/metadata-cache/status")
    assert r.status_code == 401
    # Any authenticated user works — seeded_app's admin_token is the
    # easiest valid bearer; downgrade once the test harness exposes a
    # plain-user token.
    token = seeded_app["admin_token"]
    r = c.get(
        "/api/v2/metadata-cache/status",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
