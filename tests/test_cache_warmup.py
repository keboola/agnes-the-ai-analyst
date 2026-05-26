"""Cache warmup framework — state, bg task, endpoints."""

import asyncio
from unittest.mock import patch

from app.api.cache_warmup import WarmupRunState


def test_warmup_run_state_starts_empty():
    from app.api.cache_warmup import WARMUP_STATE
    assert WARMUP_STATE is None or WARMUP_STATE.completed_at is not None


def test_warmup_skips_when_env_set(monkeypatch):
    """AGNES_SKIP_CACHE_WARMUP=1 → background warmup is a no-op."""
    monkeypatch.setenv("AGNES_SKIP_CACHE_WARMUP", "1")
    from app.api import cache_warmup

    # When the env opt-out is set, maybe_schedule_startup_warmup must
    # NOT call _warm_catalog_caches_bg.
    with patch.object(cache_warmup, "_warm_catalog_caches_bg") as mock_bg:
        cache_warmup.maybe_schedule_startup_warmup()
    mock_bg.assert_not_called()


def test_warmup_runs_one_per_remote_row(monkeypatch):
    """`_warm_catalog_caches_bg` calls `_warm_one` once per remote row.

    Uses asyncio.run rather than @pytest.mark.asyncio to match the
    convention in this repo (see tests/test_selective_gzip.py).
    """
    from app.api import cache_warmup

    # Stub the registry to return 3 remote BQ rows + 1 local row.
    fake_rows = [
        {"id": "r1", "query_mode": "remote", "source_type": "bigquery"},
        {"id": "r2", "query_mode": "remote", "source_type": "bigquery"},
        {"id": "r3", "query_mode": "remote", "source_type": "bigquery"},
    ]
    warmed = []

    async def fake_warm_one(row, state, sem):
        warmed.append(row["id"])

    monkeypatch.setattr(cache_warmup, "_list_remote_rows", lambda: fake_rows)
    monkeypatch.setattr(cache_warmup, "_warm_one", fake_warm_one)
    asyncio.run(cache_warmup._warm_catalog_caches_bg(trigger="manual"))

    assert sorted(warmed) == ["r1", "r2", "r3"]


def test_status_endpoint_before_first_run(seeded_app, monkeypatch):
    """GET /status returns {state: never_run} before any warmup."""
    from app.api import cache_warmup
    monkeypatch.setattr(cache_warmup, "WARMUP_STATE", None)

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.get(
        "/api/admin/cache-warmup/status",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert r.json() == {"state": "never_run"}


def test_run_endpoint_starts_warmup(seeded_app, monkeypatch):
    """POST /run schedules a warmup and returns 200."""
    from app.api import cache_warmup
    monkeypatch.setattr(cache_warmup, "WARMUP_STATE", None)
    # Patch the actual warmup so the test doesn't run a real one.
    monkeypatch.setattr(cache_warmup, "_warm_catalog_caches_bg",
                        lambda trigger="manual", state=None: _async_noop())

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/admin/cache-warmup/run",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200


def test_run_endpoint_returns_run_id_not_none(seeded_app, monkeypatch):
    """POST /run returns a non-null run_id even when the bg task hasn't
    started running yet (no race between create_task and the handler return)."""
    from app.api import cache_warmup

    async def fake_bg(trigger="manual", state=None):
        await asyncio.sleep(0.01)  # don't actually warm

    monkeypatch.setattr(cache_warmup, "WARMUP_STATE", None)
    monkeypatch.setattr(cache_warmup, "_warm_catalog_caches_bg", fake_bg)

    c = seeded_app["client"]
    token = seeded_app["admin_token"]
    r = c.post(
        "/api/admin/cache-warmup/run",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "started"
    assert body["run_id"] is not None
    assert len(body["run_id"]) == 8  # uuid4 hex prefix


def test_list_remote_rows_filters_to_bigquery_source_type(monkeypatch):
    """Devin Review #1 regression: `_list_remote_rows` previously returned
    every `query_mode='remote'` row regardless of `source_type`. The downstream
    `_warm_schema_sync` always calls `get_bq_access()`, so a non-BQ remote row
    (hypothetical today, plausible as connectors expand) would crash the
    warmup pass.

    Fix: filter on `source_type == 'bigquery'` in `_list_remote_rows` so the
    BQ-only warmup path only sees rows it can handle. Rows from other sources
    are simply skipped — they'll grow their own warmup paths as needed."""
    from app.api import cache_warmup

    fake_rows = [
        {"id": "bq_remote", "query_mode": "remote", "source_type": "bigquery"},
        {"id": "kbc_remote", "query_mode": "remote", "source_type": "keboola"},
        {"id": "bq_local", "query_mode": "local", "source_type": "bigquery"},
        {"id": "future_remote", "query_mode": "remote", "source_type": "snowflake"},
        {"id": "bq_remote2", "query_mode": "remote", "source_type": "bigquery"},
    ]

    class FakeRepo:
        def list_all(self):
            return fake_rows

    fake = FakeRepo()
    monkeypatch.setattr("app.api.cache_warmup.table_registry_repo", lambda: fake)

    result = cache_warmup._list_remote_rows()
    ids = sorted(r["id"] for r in result)
    assert ids == ["bq_remote", "bq_remote2"], (
        f"only remote+bigquery rows should be warmed, got {ids}"
    )


async def _async_noop():
    return None
