"""POST /api/query cost guardrail for query_mode='remote' BigQuery rows.

When user SQL references a registered remote-BQ name (or a direct
`bq."<ds>"."<tbl>"` path), run a BQ dry-run before execute. If the
estimated scan exceeds the configured cap, reject with 400 +
`remote_scan_too_large` so the operator pivots to `da fetch`.

Default cap: 5 GiB per request. Configurable via
`api.query.bq_max_scan_bytes` in /admin/server-config (#160 §4.4).
"""
from __future__ import annotations

import pytest


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _register_bq_remote_row(name: str, bucket: str, source_table: str) -> None:
    from src.db import get_system_db
    from src.repositories.table_registry import TableRegistryRepository
    sys_conn = get_system_db()
    try:
        TableRegistryRepository(sys_conn).register(
            id=f"bq.{bucket}.{source_table}",
            name=name,
            source_type="bigquery",
            bucket=bucket,
            source_table=source_table,
            query_mode="remote",
        )
    finally:
        sys_conn.close()


@pytest.fixture
def mock_dry_run(monkeypatch):
    """Replace `_bq_dry_run_bytes` with a controllable stub. Each test sets
    `mock_dry_run["bytes"]` to control what /api/query sees. Also stubs
    `get_bq_access` so the guardrail doesn't require a real BQ connection
    in the test env."""
    state = {"bytes": 0}

    def fake_dry_run(*args, **kwargs):
        return state["bytes"]

    monkeypatch.setattr("app.api.query._bq_dry_run_bytes", fake_dry_run, raising=False)

    # Stub get_bq_access so the guardrail's BqAccess construction doesn't
    # fail with `not_configured` in tests that don't set up real BQ.
    class _FakeProjects:
        data = "test-data-prj"
        billing = "test-billing-prj"

    class _FakeBqAccess:
        projects = _FakeProjects()

    monkeypatch.setattr(
        "app.api.query.get_bq_access",
        lambda: _FakeBqAccess(),
        raising=False,
    )
    return state


def test_query_under_cap_calls_dry_run(seeded_app, mock_dry_run, monkeypatch):
    """Dry-run is invoked when SQL references a registered remote BQ row.
    Use a sentinel side-effect to confirm: the mock records call counts."""
    _register_bq_remote_row("ue", "finance", "ue")
    state = mock_dry_run
    state["bytes"] = 1 * 1024 * 1024  # 1 MiB
    state["call_count"] = 0

    def counting_fake(*args, **kwargs):
        state["call_count"] += 1
        return state["bytes"]

    monkeypatch.setattr("app.api.query._bq_dry_run_bytes", counting_fake, raising=False)

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    c.post(
        "/api/query",
        json={"sql": "SELECT count(*) FROM ue"},
        headers=_auth(token),
    )
    assert state["call_count"] >= 1, \
        "guardrail must invoke _bq_dry_run_bytes when SQL references a registered remote BQ row"


def test_query_over_cap_rejected_400(seeded_app, mock_dry_run, monkeypatch):
    """Dry-run reports 10 GiB; default cap (5 GiB) is exceeded → 400 with
    structured detail naming bytes + tables + suggestion."""
    _register_bq_remote_row("ue", "finance", "ue")
    mock_dry_run["bytes"] = 10 * 1024 * 1024 * 1024  # 10 GiB

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/query",
        json={"sql": "SELECT * FROM ue"},
        headers=_auth(token),
    )
    assert r.status_code == 400, r.json()
    detail = r.json().get("detail", {})
    if isinstance(detail, dict):
        assert detail.get("reason") == "remote_scan_too_large", detail
        assert detail.get("scan_bytes") >= 10 * 1024 * 1024 * 1024
        assert "da fetch" in detail.get("suggestion", "").lower() or \
               "fetch" in detail.get("suggestion", "").lower()
        assert "ue" in detail.get("tables", []) or \
               any("ue" in t for t in detail.get("tables", []))


def test_no_bq_row_reference_skips_dry_run(seeded_app, monkeypatch):
    """A query that doesn't touch any registered BQ remote row must NOT
    invoke `_bq_dry_run_bytes` — guardrail incurs zero new latency on
    plain non-BQ queries."""
    state = {"calls": 0}

    def counting_fake(*args, **kwargs):
        state["calls"] += 1
        return 100 * 1024 * 1024 * 1024  # 100 GiB — irrelevant if not called

    monkeypatch.setattr("app.api.query._bq_dry_run_bytes", counting_fake, raising=False)

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    c.post(
        "/api/query",
        json={"sql": "SELECT 1 AS x"},
        headers=_auth(token),
    )
    assert state["calls"] == 0, \
        f"guardrail must skip dry-run on non-BQ queries; got {state['calls']} calls"
