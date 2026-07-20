"""End-to-end tests for the three bq_metadata_refresh endpoints."""

import logging
import re
from unittest.mock import patch

from app.api._metadata_models import TableMetadata


def _per_table_lines(caplog):
    """The per-table timing INFO lines emitted by run_bq_metadata_refresh."""
    return [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("bq metadata refresh table:")
    ]


def _field(line: str, name: str) -> str:
    """Pull a ``name=value`` token out of a per-table log line."""
    m = re.search(rf"{name}=(\S+)", line)
    assert m is not None, f"no {name}= in {line!r}"
    return m.group(1)


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


def test_run_refresh_returns_run_id_and_started_at(seeded_app):
    """Issue #256: response now carries `run_id` + `started_at` so two
    log streams (server + client) can correlate against the same run."""
    _register_remote(seeded_app, "for_run_id")
    fake = TableMetadata(rows=1, size_bytes=1)
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    with patch("connectors.bigquery.metadata.fetch", return_value=fake):
        r = c.post(
            "/api/admin/run-bq-metadata-refresh",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "run_id" in body and len(body["run_id"]) == 8
    assert "started_at" in body and body["started_at"]


def test_concurrent_refresh_returns_409_already_running(seeded_app):
    """Issue #256: second concurrent POST receives 409 instead of doing
    duplicate BQ work. Implemented via module-level asyncio.Lock."""
    import asyncio
    import httpx

    from app.api import bq_metadata_refresh as mod

    _register_remote(seeded_app, "concurrent_t")
    c = seeded_app["client"]
    token = seeded_app["admin_token"]

    # Simulate "refresh in flight" by holding the module-level lock
    # ourselves and asserting the endpoint returns 409 immediately.
    # `asyncio.Lock` requires a running loop to acquire; use a fresh one.
    async def _hold_lock_and_call():
        async with mod._refresh_lock:
            mod._refresh_state["run_id"] = "abcd1234"
            mod._refresh_state["started_at"] = "2026-05-12T13:00:00+00:00"
            try:
                # Call via TestClient (sync) — locking is module-level so the
                # endpoint handler sees the lock held.
                return c.post(
                    "/api/admin/run-bq-metadata-refresh",
                    headers={"Authorization": f"Bearer {token}"},
                )
            finally:
                mod._refresh_state["run_id"] = None
                mod._refresh_state["started_at"] = None

    r = asyncio.new_event_loop().run_until_complete(_hold_lock_and_call())
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["reason"] == "already_running"
    assert detail["run_id"] == "abcd1234"
    assert detail["started_at"] == "2026-05-12T13:00:00+00:00"


# ─── Per-table timing ───────────────────────────────


def test_run_refresh_logs_per_table_timing(seeded_app, caplog):
    """Every table gets one INFO line carrying fetch_ms + total_ms so a slow
    refresh cycle is attributable to specific tables from the logs alone."""
    _register_remote(seeded_app, "timed_a")
    _register_remote(seeded_app, "timed_b")

    fake = TableMetadata(rows=5, size_bytes=512)
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    with caplog.at_level(logging.INFO, logger="app.api.bq_metadata_refresh"):
        with patch("connectors.bigquery.metadata.fetch", return_value=fake):
            r = c.post(
                "/api/admin/run-bq-metadata-refresh",
                headers={"Authorization": f"Bearer {token}"},
            )
    assert r.status_code == 200, r.text

    lines = _per_table_lines(caplog)
    for tid in ("timed_a", "timed_b"):
        line = next(m for m in lines if f"table_id={tid}" in m)
        assert "status=ok" in line
        # Both timings render as non-negative ints (not "None").
        for field in ("fetch_ms", "total_ms"):
            value = _field(line, field)
            assert value != "None", f"{field} unexpectedly None in {line!r}"
            assert int(value) >= 0


def test_run_refresh_per_table_log_on_error_has_fetch_ms(seeded_app, caplog):
    """On the provider-exception path the fetch was attempted, so the
    per-table line reports status=error with a real (non-None) fetch_ms."""
    _register_remote(seeded_app, "boom_timed")

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    with caplog.at_level(logging.INFO, logger="app.api.bq_metadata_refresh"):
        with patch(
            "connectors.bigquery.metadata.fetch",
            side_effect=RuntimeError("BQ throttle"),
        ):
            r = c.post(
                "/api/admin/run-bq-metadata-refresh",
                headers={"Authorization": f"Bearer {token}"},
            )
    assert r.status_code == 200

    line = next(m for m in _per_table_lines(caplog) if "table_id=boom_timed" in m)
    assert "status=error" in line
    assert _field(line, "fetch_ms") != "None"
    assert int(_field(line, "total_ms")) >= 0


def test_refresh_one_returns_timing_fields_on_success(seeded_app):
    """refresh_one carries fetch_ms/total_ms as ints on the happy path —
    these also pass straight through the single-row refresh endpoint."""
    from app.api.bq_metadata_refresh import refresh_one

    fake = TableMetadata(rows=1, size_bytes=1)
    with patch("connectors.bigquery.metadata.fetch", return_value=fake):
        out = refresh_one({"id": "one_timed", "bucket": "dwh_base", "source_table": "one_timed"})
    assert out["status"] == "ok"
    assert isinstance(out["fetch_ms"], int) and out["fetch_ms"] >= 0
    assert isinstance(out["total_ms"], int) and out["total_ms"] >= 0


def test_refresh_one_invalid_identifier_has_null_fetch_ms(seeded_app):
    """The identifier-validation early return never calls fetch, so
    fetch_ms is None while total_ms still reports the (tiny) wall time."""
    from app.api.bq_metadata_refresh import refresh_one

    out = refresh_one({"id": "bad_ident", "bucket": "bad bucket", "source_table": "t"})
    assert out["status"] == "error"
    assert out["fetch_ms"] is None
    assert isinstance(out["total_ms"], int) and out["total_ms"] >= 0


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
