"""Admin API accepts source_query when query_mode='materialized', rejects
mismatches between mode and query field.

Tests that hit the remote-mode register path require `stub_bq_extractor`
to bypass the post-register rebuild's real-BQ traffic. Materialized-only
tests skip the BG path (the 201 fast-path returns before any rebuild
fires) so they don't need the stub.

Covers PR #145 (re-implementation against 0.24.0 base):
- RegisterTableRequest + UpdateTableRequest model_validators
- _validate_bigquery_register_payload materialized branch (skips bucket/
  source_table checks, requires source_query)
- register_table 201 response for materialized BQ rows (no synchronous
  materialize — cron tick or manual /api/sync/trigger picks them up)
- update_table clears stale source_query when switching mode away from
  materialized

Shares the seeded_app + bq_instance fixtures from conftest /
test_admin_bq_register.py for parity with the existing BQ test surface.
"""
import pytest


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _materialized_payload(**overrides):
    p = {
        "name": "orders_90d",
        "source_type": "bigquery",
        "query_mode": "materialized",
        # BQ-native or DuckDB-flavor SQL — both accepted since Task 2 wraps
        # materialized SQL in bigquery_query() (BQ jobs API path). Backtick
        # identifiers are now allowed for materialized rows; remote/local rows
        # still require DuckDB-flavor (double-quoted) identifiers.
        "source_query": 'SELECT date FROM bq."ds"."orders"',
        "sync_schedule": "every 6h",
    }
    p.update(overrides)
    return p


def test_register_materialized_requires_source_query(seeded_app, bq_instance):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/admin/register-table",
        json={
            "name": "missing_query",
            "source_type": "bigquery",
            "query_mode": "materialized",
            # source_query missing
        },
        headers=_auth(token),
    )
    assert 400 <= r.status_code < 500, r.json()
    detail = str(r.json().get("detail", "")).lower()
    assert "source_query" in detail or "materialized" in detail


def test_register_materialized_accepts_source_query(seeded_app, bq_instance):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/admin/register-table",
        json=_materialized_payload(name="orders_90d_a"),
        headers=_auth(token),
    )
    assert r.status_code == 201, r.json()
    body = r.json()
    assert body["status"] == "registered"
    assert "Materialized" in body.get("message", "")


def test_register_remote_rejects_source_query(seeded_app, bq_instance):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/admin/register-table",
        json={
            "name": "live_orders",
            "source_type": "bigquery",
            "bucket": "analytics",
            "source_table": "orders",
            "query_mode": "remote",
            "source_query": "SELECT 1",
        },
        headers=_auth(token),
    )
    assert 400 <= r.status_code < 500, r.json()


def test_register_local_rejects_source_query(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/admin/register-table",
        json={
            "name": "kbc_orders",
            "source_type": "keboola",
            "query_mode": "local",
            "source_query": "SELECT 1",
        },
        headers=_auth(token),
    )
    assert 400 <= r.status_code < 500, r.json()


def test_register_materialized_with_empty_source_query_rejected(seeded_app, bq_instance):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/admin/register-table",
        json=_materialized_payload(name="empty_q", source_query=""),
        headers=_auth(token),
    )
    assert 400 <= r.status_code < 500, r.json()


def test_update_source_query_alone_requires_query_mode(seeded_app, bq_instance, stub_bq_extractor):
    """PUT body with source_query but no query_mode is incoherent — reject
    so non-materialized rows can't carry an orphan source_query."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]

    # Seed a remote-mode row.
    r = c.post(
        "/api/admin/register-table",
        json={
            "name": "live_orphan",
            "source_type": "bigquery",
            "bucket": "analytics",
            "source_table": "orders",
            "query_mode": "remote",
        },
        headers=_auth(token),
    )
    assert r.status_code in (200, 202), r.json()  # synchronous or async
    table_id = r.json()["id"]

    r2 = c.put(
        f"/api/admin/registry/{table_id}",
        json={"source_query": "SELECT 1"},
        headers=_auth(token),
    )
    assert 400 <= r2.status_code < 500, r2.json()


def test_update_schedule_only_on_materialized_row_succeeds(
    seeded_app, bq_instance, stub_bq_extractor,
):
    """REGRESSION (Devin BUG_0002 on 2219255): an admin editing only the
    sync_schedule of a materialized row sends `{query_mode: 'materialized',
    sync_schedule: '...'}` (the Edit modal always sends query_mode for BQ
    rows). Pre-fix the UpdateTableRequest validator rejected this with 422
    because source_query wasn't in the body — even though the existing row
    already had one.

    The PUT semantics overlay the body on the existing row, so omitted
    source_query keeps the stored value. The synthetic RegisterTableRequest
    constructed against the merged record at the handler still runs the
    strict cross-field check, so the truly-broken case (materialized
    without ANY source_query, even on existing) is still caught."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]

    # Seed a materialized row with a real source_query.
    r = c.post("/api/admin/register-table", json={
        "name": "schedule_edit_target",
        "source_type": "bigquery",
        "query_mode": "materialized",
        "source_query": "SELECT 1",
        "sync_schedule": "every 1h",
    }, headers=_auth(token))
    assert r.status_code == 201, r.json()
    table_id = r.json()["id"]

    # Edit ONLY the schedule. UI's saveTableEdit sends query_mode for BQ
    # rows even when the operator didn't change it.
    r2 = c.put(f"/api/admin/registry/{table_id}", json={
        "query_mode": "materialized",
        "sync_schedule": "every 12h",
    }, headers=_auth(token))
    assert r2.status_code == 200, r2.json()

    # Verify the schedule changed and source_query survived.
    r3 = c.get("/api/admin/registry", headers=_auth(token))
    row = next((t for t in r3.json()["tables"] if t["id"] == table_id), None)
    assert row is not None
    assert row["sync_schedule"] == "every 12h"
    assert row["source_query"] == "SELECT 1"  # preserved across edit
    assert row["query_mode"] == "materialized"


def test_update_materialized_with_explicit_empty_source_query_rejected(
    seeded_app, bq_instance, stub_bq_extractor,
):
    """The fix above relaxes the validator for OMITTED source_query, but
    explicitly setting it to an empty / whitespace string while claiming
    materialized is still a typo and must be rejected (not silently
    persisted as NULL)."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]

    r = c.post("/api/admin/register-table", json={
        "name": "explicit_empty",
        "source_type": "bigquery",
        "query_mode": "materialized",
        "source_query": "SELECT 1",
    }, headers=_auth(token))
    assert r.status_code == 201, r.json()
    table_id = r.json()["id"]

    r2 = c.put(f"/api/admin/registry/{table_id}", json={
        "query_mode": "materialized",
        "source_query": "",  # explicitly empty
    }, headers=_auth(token))
    assert 400 <= r2.status_code < 500, r2.json()


def test_update_materialized_to_remote_clears_source_query(
    seeded_app, bq_instance, stub_bq_extractor,
):
    """When admin switches a materialized table to remote/local, the stale
    source_query must be cleared in the DB — otherwise the registry shows
    a non-materialized row carrying an orphan SQL body."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]

    # Seed a materialized table with a source_query.
    r = c.post(
        "/api/admin/register-table",
        json=_materialized_payload(name="switcher"),
        headers=_auth(token),
    )
    assert r.status_code == 201, r.json()
    table_id = r.json()["id"]

    # Switch to remote — must include bucket+source_table for the new mode
    # (the merged validator runs the BQ payload check on the merged record).
    r2 = c.put(
        f"/api/admin/registry/{table_id}",
        json={
            "query_mode": "remote",
            "bucket": "analytics",
            "source_table": "orders_90d",
        },
        headers=_auth(token),
    )
    assert r2.status_code == 200, r2.json()

    # Verify in the registry: query_mode flipped, source_query cleared.
    r3 = c.get("/api/admin/registry", headers=_auth(token))
    assert r3.status_code == 200, r3.json()
    row = next((t for t in r3.json()["tables"] if t["id"] == table_id), None)
    assert row is not None, f"Table {table_id} not found in registry"
    assert row["query_mode"] == "remote"
    assert row["source_query"] in (None, "")


def test_register_materialized_persists_source_query_in_registry(seeded_app, bq_instance):
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/admin/register-table",
        json=_materialized_payload(
            name="persist_q",
            source_query='SELECT col FROM bq."ds"."t" WHERE x = 1',
        ),
        headers=_auth(token),
    )
    assert r.status_code == 201, r.json()
    table_id = r.json()["id"]

    r2 = c.get("/api/admin/registry", headers=_auth(token))
    row = next((t for t in r2.json()["tables"] if t["id"] == table_id), None)
    assert row is not None
    assert row["query_mode"] == "materialized"
    assert "WHERE x = 1" in row["source_query"]


# --- Backtick (BigQuery-native) source_query handling ------------------------
#
# Task 2 (materialize-sync-fix) changed the BQ materialization path to run
# admin SQL through the BQ jobs API (bigquery_query() wrapper) rather than
# through DuckDB's BQ extension COPY path. BQ-native SQL requires backticks
# for dashed project/dataset/table identifiers. The backtick guard has been
# relaxed for ALL materialized rows: the validator now only rejects backticks
# for remote/local rows (DuckDB-flavor SQL contract). Materialized rows must
# be allowed to carry backticks so operators can reference dashed identifiers.
# See test_admin_validator_backtick_relaxed_for_materialized.py for the
# model-layer unit tests.


def test_register_materialized_accepts_backtick_source_query(seeded_app, bq_instance, stub_bq_extractor):
    """BQ materialized rows now accept BQ-native backtick syntax; the
    materialize path (Task 2) wraps them in bigquery_query() which uses
    the BQ jobs API — not DuckDB's COPY — so backticks are valid."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/admin/register-table",
        json=_materialized_payload(
            name="bt_native",
            source_query="SELECT * FROM `prj-grp.ds.product_inventory`",
        ),
        headers=_auth(token),
    )
    assert r.status_code in (200, 201, 202), r.json()
    reg = c.get("/api/admin/registry", headers=_auth(token)).json()
    row = next(t for t in reg["tables"] if t["id"] == "bt_native")
    assert row["source_query"] == "SELECT * FROM `prj-grp.ds.product_inventory`"


def test_update_materialized_accepts_backtick_source_query(
    seeded_app, bq_instance, stub_bq_extractor,
):
    """PUT to a materialized BQ row may switch source_query to BQ-native
    backtick form — accepted now that Task 2 wraps via jobs API."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]

    r = c.post(
        "/api/admin/register-table",
        json=_materialized_payload(
            name="bt_update",
            source_query='SELECT * FROM bq."ds"."t"',
        ),
        headers=_auth(token),
    )
    assert r.status_code == 201, r.json()
    table_id = r.json()["id"]

    # PATCH the source_query to a BQ-native backtick form — now accepted.
    r2 = c.put(
        f"/api/admin/registry/{table_id}",
        json={
            "query_mode": "materialized",
            "source_query": "SELECT * FROM `prj.ds.t`",
        },
        headers=_auth(token),
    )
    assert r2.status_code == 200, r2.json()
    reg = c.get("/api/admin/registry", headers=_auth(token)).json()
    row = next(t for t in reg["tables"] if t["id"] == table_id)
    assert row["source_query"] == "SELECT * FROM `prj.ds.t`"


def test_register_materialized_keboola_accepts_backtick_source_query(seeded_app):
    """Keboola materialized rows also accept backtick source_query at register
    time — the backtick guard now only applies to remote/local rows. If the
    SQL is invalid at runtime (DuckDB parse error), that surfaces as a sync
    error, not a registration error."""
    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/admin/register-table",
        json={
            "name": "kbc_bt",
            "source_type": "keboola",
            "query_mode": "materialized",
            "source_query": "SELECT * FROM `bucket.table`",
        },
        headers=_auth(token),
    )
    assert r.status_code == 201, r.json()


# --- Surface materialize errors per-row ---------------------------------------
#
# Errors that bubble out of `_run_materialized_pass` per-row used to disappear
# into scheduler stderr. Operators have no API surface to find out WHY a row
# isn't materializing. The trigger pass now writes the failure into
# `sync_state.error` (existing column) so `GET /api/admin/registry` can include
# `last_sync_error` per row, exposed to `da admin status` / the admin UI.


def test_run_materialized_pass_surfaces_error_in_sync_state(seeded_app, bq_instance):
    """When a per-row materialize call raises, `_run_materialized_pass` writes
    the error to sync_state.error so it can be surfaced via the registry API.
    """
    from app.api.sync import _run_materialized_pass
    from src.repositories.sync_state import SyncStateRepository
    from src.repositories.table_registry import TableRegistryRepository
    from src.db import get_system_db

    sys_conn = get_system_db()
    try:
        # Seed a materialized BQ row.
        TableRegistryRepository(sys_conn).register(
            id="boom",
            name="boom",
            source_type="bigquery",
            query_mode="materialized",
            source_query='SELECT * FROM bq."ds"."missing"',
            sync_schedule="every 1m",
        )

        # Stub the materialize seam so the per-row branch raises.
        from unittest.mock import patch
        with patch(
            "app.api.sync._materialize_table",
            side_effect=RuntimeError("boom: missing table"),
        ):
            summary = _run_materialized_pass(sys_conn, bq=None)

        assert any(e["table"] == "boom" for e in summary["errors"]), summary

        state = SyncStateRepository(sys_conn).get_table_state("boom")
        assert state is not None, "sync_state row should be created on error"
        assert (state.get("status") or "") == "error"
        assert "boom: missing table" in (state.get("error") or "")
    finally:
        # Cleanup so the next test starts clean.
        try:
            sys_conn.execute("DELETE FROM table_registry WHERE id='boom'")
            sys_conn.execute("DELETE FROM sync_state WHERE table_id='boom'")
        except Exception:
            pass
        sys_conn.close()


def test_run_materialized_pass_clears_error_on_success(seeded_app, bq_instance):
    """When a row that previously errored materializes cleanly, the prior
    sync_state.error is cleared so the registry response stops surfacing
    a stale failure message."""
    from app.api.sync import _run_materialized_pass
    from src.repositories.sync_state import SyncStateRepository
    from src.repositories.table_registry import TableRegistryRepository
    from src.db import get_system_db

    sys_conn = get_system_db()
    try:
        TableRegistryRepository(sys_conn).register(
            id="recover",
            name="recover",
            source_type="bigquery",
            query_mode="materialized",
            source_query='SELECT * FROM bq."ds"."t"',
            sync_schedule="every 1m",
        )

        # Pre-seed sync_state with an error so we can verify it gets cleared.
        SyncStateRepository(sys_conn).set_error("recover", "previous run failed")
        state_before = SyncStateRepository(sys_conn).get_table_state("recover")
        assert (state_before.get("status") or "") == "error"

        from unittest.mock import patch
        # Successful materialize returns a stats dict.
        with patch(
            "app.api.sync._materialize_table",
            return_value={
                "rows": 5, "size_bytes": 100, "hash": "abc123",
                "query_mode": "materialized",
            },
        ):
            summary = _run_materialized_pass(sys_conn, bq=None)

        assert "recover" in summary["materialized"], summary
        state_after = SyncStateRepository(sys_conn).get_table_state("recover")
        assert (state_after.get("status") or "") == "ok"
        assert (state_after.get("error") or "") == ""
    finally:
        try:
            sys_conn.execute("DELETE FROM table_registry WHERE id='recover'")
            sys_conn.execute("DELETE FROM sync_state WHERE table_id='recover'")
        except Exception:
            pass
        sys_conn.close()


def test_get_registry_exposes_last_sync_error_per_table(seeded_app, bq_instance):
    """GET /api/admin/registry includes `last_sync_error` populated from
    sync_state.error so operators have a UI/API surface to see why a
    materialize is failing without trawling scheduler logs."""
    from src.repositories.sync_state import SyncStateRepository
    from src.repositories.table_registry import TableRegistryRepository
    from src.db import get_system_db

    sys_conn = get_system_db()
    try:
        TableRegistryRepository(sys_conn).register(
            id="failing_row",
            name="failing_row",
            source_type="bigquery",
            query_mode="materialized",
            source_query='SELECT * FROM bq."ds"."t"',
        )
        SyncStateRepository(sys_conn).set_error(
            "failing_row", "USER_PROJECT_DENIED on project xxx",
        )
    finally:
        sys_conn.close()

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get("/api/admin/registry", headers=_auth(token))
    assert r.status_code == 200, r.json()
    row = next(
        (t for t in r.json()["tables"] if t["id"] == "failing_row"), None,
    )
    assert row is not None, r.json()
    assert "last_sync_error" in row, list(row.keys())
    assert "USER_PROJECT_DENIED" in (row["last_sync_error"] or "")
