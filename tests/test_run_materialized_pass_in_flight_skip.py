"""When materialize_query raises MaterializeInFlightError, _run_materialized_pass
must record it as a 'skipped, in_flight' outcome and NOT call state.set_error
(otherwise sync_state surfaces a false-positive 'failure' for a healthy
in-progress run)."""

from __future__ import annotations
from unittest.mock import MagicMock, patch

import pytest

from app.api.sync import _run_materialized_pass
from connectors.bigquery.extractor import MaterializeInFlightError


@pytest.fixture
def fake_registry_with_one_materialized(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    rows = [
        {
            "id": "in_flight_t",
            "name": "in_flight_t",
            "query_mode": "materialized",
            "source_type": "bigquery",
            "source_query": "SELECT * FROM `ds.t`",
            "sync_schedule": None,
        }
    ]

    class _Repo:
        def __init__(self, conn):
            pass

        def list_all(self):
            return rows

    class _State:
        def __init__(self, conn):
            self.set_error_calls = []
            self.set_skipped_calls = []
            self.update_sync_calls = []

        def get_last_sync(self, _id):
            return None

        def set_error(self, table_id, msg):
            self.set_error_calls.append((table_id, msg))

        def set_skipped(self, table_id, reason):
            self.set_skipped_calls.append((table_id, reason))

        def update_sync(self, **kw):
            self.update_sync_calls.append(kw)

    state = _State(None)
    # Factory swap: api module imports table_registry_repo / sync_state_repo
    # from src.repositories and calls them with no args.
    fake_registry = _Repo(None)
    monkeypatch.setattr("app.api.sync.table_registry_repo", lambda: fake_registry)
    monkeypatch.setattr("app.api.sync.sync_state_repo", lambda: state)
    return state


def test_default_schedule_falls_through_env_then_every_1h(
    monkeypatch,
    fake_registry_with_one_materialized,
):
    """Per-table ``sync_schedule=None`` → fall through to
    ``AGNES_DEFAULT_SYNC_SCHEDULE`` env (operator deployment override) →
    fall through to literal ``every 1h`` (OSS-historical default).
    Test the THREE branches:

      1. Per-table schedule wins over env.
      2. Env wins when per-table is None.
      3. ``every 1h`` is the floor — env unset + per-table None.

    Branch (2) is the operator knob for ``daily 03:00`` deployments
    (data freshness budget once-per-day; the hourly default
    over-fetches Snowflake on every Keboola export-async cycle)."""
    captured = {}

    def fake_is_due(schedule, last_iso, now=None):
        captured["schedule"] = schedule
        return False  # short-circuit the dispatcher

    monkeypatch.setattr("app.api.sync.is_table_due", fake_is_due)

    # Case 3: env unset, per-table None → "every 1h"
    monkeypatch.delenv("AGNES_DEFAULT_SYNC_SCHEDULE", raising=False)
    _run_materialized_pass(MagicMock(), MagicMock())
    assert captured["schedule"] == "every 1h", captured

    # Case 2: env set, per-table None → env value
    monkeypatch.setenv("AGNES_DEFAULT_SYNC_SCHEDULE", "daily 03:00")
    _run_materialized_pass(MagicMock(), MagicMock())
    assert captured["schedule"] == "daily 03:00", captured

    # Case 1: per-table schedule wins over env. (Mutate fixture's row.)
    fake_registry_with_one_materialized  # ensure fixture is loaded
    import app.api.sync as _sm

    # The fixture's _Repo.list_all returns a captured list; reach into
    # its closure isn't easy. Easier: monkeypatch list_all directly.
    pinned_rows = [
        {
            "id": "in_flight_t",
            "name": "in_flight_t",
            "query_mode": "materialized",
            "source_type": "bigquery",
            "source_query": "SELECT 1",
            "sync_schedule": "every 30m",  # explicit per-table
        }
    ]

    class _RepoWithSched:
        def __init__(self, conn):
            pass

        def list_all(self):
            return pinned_rows

    monkeypatch.setattr(_sm, "table_registry_repo", lambda: _RepoWithSched(None))
    _run_materialized_pass(MagicMock(), MagicMock())
    assert captured["schedule"] == "every 30m", captured


def test_in_flight_recorded_as_skipped_not_error(fake_registry_with_one_materialized):
    state = fake_registry_with_one_materialized

    with patch(
        "app.api.sync._materialize_table",
        side_effect=MaterializeInFlightError("in_flight_t", layer="process"),
    ):
        summary = _run_materialized_pass(MagicMock(), MagicMock())

    assert summary["materialized"] == []
    assert summary["errors"] == []
    assert len(summary["skipped"]) == 1
    skipped = summary["skipped"][0]
    assert skipped == {"table": "in_flight_t", "reason": "in_flight"}
    assert state.set_error_calls == []
    assert state.set_skipped_calls == [("in_flight_t", "in_flight")]
    assert state.update_sync_calls == []


def test_due_check_skipped_uses_due_check_reason(fake_registry_with_one_materialized, monkeypatch):
    monkeypatch.setattr("app.api.sync.is_table_due", lambda *a, **k: False)

    summary = _run_materialized_pass(MagicMock(), MagicMock())
    assert summary["skipped"] == [{"table": "in_flight_t", "reason": "due_check"}]


# ---- targeted-trigger filter -----------------------------------------------


@pytest.fixture
def fake_registry_with_three_materialized(monkeypatch, tmp_path):
    """Three materialized rows so we can verify ``tables=['orders']`` only
    touches `orders` and skips the other two with ``reason='not_in_target'``."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    rows = [
        {
            "id": "orders",
            "name": "orders",
            "query_mode": "materialized",
            "source_type": "bigquery",
            "source_query": "SELECT 1",
            "sync_schedule": None,
        },
        {
            "id": "customers",
            "name": "customers",
            "query_mode": "materialized",
            "source_type": "bigquery",
            "source_query": "SELECT 1",
            "sync_schedule": None,
        },
        {
            "id": "events",
            "name": "events",
            "query_mode": "materialized",
            "source_type": "bigquery",
            "source_query": "SELECT 1",
            "sync_schedule": None,
        },
    ]

    class _Repo:
        def __init__(self, conn):
            pass

        def list_all(self):
            return rows

    class _State:
        def __init__(self, conn):
            pass

        def get_last_sync(self, _id):
            return None

        def set_error(self, *a, **kw):
            pass

        def set_skipped(self, *a, **kw):
            pass

        def update_sync(self, **kw):
            pass

    monkeypatch.setattr("app.api.sync.table_registry_repo", lambda: _Repo(None))
    monkeypatch.setattr("app.api.sync.sync_state_repo", lambda: _State(None))
    return rows


def test_targeted_trigger_only_processes_listed_tables(
    fake_registry_with_three_materialized,
):
    """Targeted ``tables=['orders']`` must skip 'customers' and 'events'
    even though all three are due. Pre-fix bug: targeted trigger of
    `kbc_job` re-ran every other due materialized row too because the
    pass ignored the `tables` arg entirely."""
    materialized_calls = []

    def fake_mat(table_id, sql, bq, output_dir, max_bytes):
        materialized_calls.append(table_id)
        return {"rows": 1, "size_bytes": 100, "hash": "abc"}

    with patch("app.api.sync._materialize_table", side_effect=fake_mat):
        summary = _run_materialized_pass(MagicMock(), MagicMock(), tables=["orders"])

    assert materialized_calls == ["orders"]
    assert summary["materialized"] == ["orders"]
    skipped_pairs = [(s["table"], s["reason"]) for s in summary["skipped"]]
    assert ("customers", "not_in_target") in skipped_pairs
    assert ("events", "not_in_target") in skipped_pairs


def test_targeted_trigger_matches_id_or_name(fake_registry_with_three_materialized, monkeypatch):
    """Operators may pass either the registry id or the human-friendly
    name. Both forms should select the same row."""
    monkeypatch.setattr("app.api.sync._materialize_table", lambda **kw: {"rows": 0, "size_bytes": 0, "hash": "x"})

    # By id
    s1 = _run_materialized_pass(MagicMock(), MagicMock(), tables=["orders"])
    assert s1["materialized"] == ["orders"]

    # By name (same value here, but the lookup logic checks both columns)
    s2 = _run_materialized_pass(MagicMock(), MagicMock(), tables=["events"])
    assert s2["materialized"] == ["events"]


def test_no_target_processes_all_due_rows(fake_registry_with_three_materialized):
    """Backward compat: ``tables=None`` (no filter) keeps the original
    behavior — process every due materialized row."""
    with patch("app.api.sync._materialize_table", return_value={"rows": 0, "size_bytes": 0, "hash": "x"}):
        summary = _run_materialized_pass(MagicMock(), MagicMock(), tables=None)

    assert sorted(summary["materialized"]) == ["customers", "events", "orders"]
    assert summary["skipped"] == []
