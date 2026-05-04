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
    rows = [{
        "id": "in_flight_t",
        "name": "in_flight_t",
        "query_mode": "materialized",
        "source_type": "bigquery",
        "source_query": "SELECT * FROM `ds.t`",
        "sync_schedule": None,
    }]

    class _Repo:
        def __init__(self, conn): pass
        def list_all(self): return rows

    class _State:
        def __init__(self, conn):
            self.set_error_calls = []
            self.update_sync_calls = []
        def get_last_sync(self, _id): return None
        def set_error(self, table_id, msg): self.set_error_calls.append((table_id, msg))
        def update_sync(self, **kw): self.update_sync_calls.append(kw)

    state = _State(None)
    monkeypatch.setattr("app.api.sync.TableRegistryRepository", _Repo)
    monkeypatch.setattr("app.api.sync.SyncStateRepository", lambda c: state)
    return state


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
    assert state.update_sync_calls == []


def test_due_check_skipped_uses_due_check_reason(fake_registry_with_one_materialized, monkeypatch):
    monkeypatch.setattr("app.api.sync.is_table_due", lambda *a, **k: False)

    summary = _run_materialized_pass(MagicMock(), MagicMock())
    assert summary["skipped"] == [{"table": "in_flight_t", "reason": "due_check"}]
