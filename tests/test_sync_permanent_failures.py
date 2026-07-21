"""Permanent upstream failures must not keep the data-refresh job red forever.

A registered table whose upstream object was deleted (Keboola Storage
``storage.tables.notFound`` → HTTP 404) fails on every retry by definition —
no amount of re-running heals it; only re-pointing or unregistering the row
does. Before this change any such per-table failure flipped ``_run_sync`` to
``False``, the ``data-refresh`` job finalized ``failed``, and the queue showed
a permanently red job even though every other table synced fine — masking
real (transient) failures from monitoring.

Contract:
  - ``_is_permanent_upstream_error`` classifies the exception.
  - ``_run_materialized_pass`` stamps ``permanent: True`` on the error entry
    (``sync_state.set_error`` still records it — per-table visibility stays).
  - ``_run_sync`` returns ``False`` only when at least one *transient*
    failure was collected; permanent-only runs return ``True`` (job 'done'),
    but the operator webhook notifier still fires with the failed tables.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from connectors.keboola.storage_api import StorageApiError


# ---- classifier -------------------------------------------------------------


def test_classifier_storage_404_is_permanent():
    from app.api.sync import _is_permanent_upstream_error

    exc = StorageApiError("table gone", status=404, body={"code": "storage.tables.notFound"})
    assert _is_permanent_upstream_error(exc) is True


def test_classifier_storage_5xx_is_transient():
    from app.api.sync import _is_permanent_upstream_error

    assert _is_permanent_upstream_error(StorageApiError("boom", status=500)) is False
    assert _is_permanent_upstream_error(StorageApiError("no status")) is False


def test_classifier_generic_exception_is_transient():
    from app.api.sync import _is_permanent_upstream_error

    assert _is_permanent_upstream_error(RuntimeError("anything")) is False


# ---- materialized pass stamps the flag --------------------------------------


@pytest.fixture
def fake_registry_one_row(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    rows = [
        {
            "id": "dead_t",
            "name": "dead_t",
            "query_mode": "materialized",
            "source_type": "bigquery",
            "source_query": "SELECT 1",
            "sync_schedule": None,
        }
    ]

    class _Repo:
        def __init__(self, conn):
            pass

        def list_all(self):
            return rows

    class _State:
        def __init__(self):
            self.set_error_calls = []

        def get_last_sync(self, _id):
            return None

        def set_error(self, table_id, msg):
            self.set_error_calls.append((table_id, msg))

        def set_skipped(self, table_id, reason):
            pass

        def update_sync(self, **kw):
            pass

    state = _State()
    monkeypatch.setattr("app.api.sync.table_registry_repo", lambda: _Repo(None))
    monkeypatch.setattr("app.api.sync.sync_state_repo", lambda: state)
    return state


def test_materialized_pass_flags_permanent_on_upstream_404(fake_registry_one_row):
    from app.api.sync import _run_materialized_pass

    state = fake_registry_one_row
    with patch(
        "app.api.sync._materialize_table",
        side_effect=StorageApiError(
            "HTTP 404: table not found upstream",
            status=404,
            body={"code": "storage.tables.notFound"},
        ),
    ):
        summary = _run_materialized_pass(MagicMock(), MagicMock())

    assert len(summary["errors"]) == 1
    entry = summary["errors"][0]
    assert entry["table"] == "dead_t"
    assert entry["permanent"] is True
    # Per-table visibility must survive: sync_state still records the error.
    assert state.set_error_calls and state.set_error_calls[0][0] == "dead_t"


def test_materialized_pass_no_flag_on_transient_error(fake_registry_one_row):
    from app.api.sync import _run_materialized_pass

    with patch(
        "app.api.sync._materialize_table",
        side_effect=StorageApiError("HTTP 503: upstream busy", status=503),
    ):
        summary = _run_materialized_pass(MagicMock(), MagicMock())

    assert len(summary["errors"]) == 1
    assert summary["errors"][0].get("permanent") is not True


# ---- _run_sync return semantics ----------------------------------------------


def _seed_bq_only_registry(tmp_path):
    import duckdb

    from src.db import _ensure_schema
    from src.repositories.table_registry import TableRegistryRepository

    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)
    TableRegistryRepository(conn).register(
        id="m1",
        name="m1",
        source_type="bigquery",
        query_mode="materialized",
        source_query="SELECT 1",
        sync_schedule="every 1m",
    )
    conn.close()


def _patch_bq_only(monkeypatch):
    monkeypatch.setattr("app.instance_config.get_data_source_type", lambda: "bigquery")
    monkeypatch.setattr(
        "app.instance_config.get_value",
        lambda *args, **kw: "my-bq-proj" if (args and args[-1] == "project") else kw.get("default", ""),
    )


class _OrchStub:
    def rebuild(self):
        return {}


def _run_sync_with_errors(tmp_path, monkeypatch, errors):
    _seed_bq_only_registry(tmp_path)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    _patch_bq_only(monkeypatch)

    from app.api import sync as sync_mod

    monkeypatch.setattr(
        "app.api.sync._run_materialized_pass",
        lambda _c, _b, *, tables=None, source_type=None: {
            "materialized": [],
            "skipped": [],
            "errors": errors,
        },
    )
    monkeypatch.setattr("src.orchestrator.SyncOrchestrator", lambda *a, **kw: _OrchStub())

    captured = {}

    def _spy_notify(*, failed_tables, fatal):
        captured["failed_tables"] = failed_tables
        captured["fatal"] = fatal

    monkeypatch.setattr("app.services.sync_notifier.notify_sync_failure", _spy_notify)
    return sync_mod._run_sync(), captured


def test_run_sync_permanent_only_returns_true_but_still_notifies(tmp_path, monkeypatch):
    ok, captured = _run_sync_with_errors(
        tmp_path,
        monkeypatch,
        [{"table": "dead_t", "error": "HTTP 404: gone upstream", "permanent": True}],
    )
    assert ok is True, "permanent-only failures must not fail the data-refresh job"
    assert captured.get("failed_tables"), "operator notifier must still fire"


def test_run_sync_transient_failure_still_returns_false(tmp_path, monkeypatch):
    ok, _ = _run_sync_with_errors(
        tmp_path,
        monkeypatch,
        [{"table": "flaky_t", "error": "HTTP 503: upstream busy"}],
    )
    assert ok is False


def test_run_sync_mixed_failures_returns_false(tmp_path, monkeypatch):
    ok, captured = _run_sync_with_errors(
        tmp_path,
        monkeypatch,
        [
            {"table": "dead_t", "error": "HTTP 404: gone upstream", "permanent": True},
            {"table": "flaky_t", "error": "HTTP 503: upstream busy"},
        ],
    )
    assert ok is False, "a transient failure among permanent ones must still fail the job"
    assert len(captured.get("failed_tables", [])) == 2
