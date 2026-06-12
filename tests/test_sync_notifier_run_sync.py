"""Integration: app.api.sync._run_sync wires the webhook notifier on failure.

Two failure surfaces are covered:
  - the outer ``except`` (fatal path) — one webhook POST naming the exception;
  - non-empty ``mat_summary['errors']`` (per-table errors) — POST lists them.

And the negatives: unset URL → no POST; a webhook that raises → sync still
completes (best-effort).
"""

import duckdb
import pytest

from src.db import _ensure_schema
from src.repositories.table_registry import TableRegistryRepository


def _seed_bq_only_registry(tmp_path):
    """A single materialized BQ row so _run_sync runs the materialized pass +
    orchestrator rebuild without spawning the Keboola extractor subprocess."""
    db_path = tmp_path / "system.duckdb"
    conn = duckdb.connect(str(db_path))
    _ensure_schema(conn)
    repo = TableRegistryRepository(conn)
    repo.register(
        id="m1",
        name="m1",
        source_type="bigquery",
        query_mode="materialized",
        source_query="SELECT 1",
        sync_schedule="every 1m",
    )
    conn.close()


def _patch_bq_only(monkeypatch):
    monkeypatch.setattr(
        "app.instance_config.get_data_source_type", lambda: "bigquery"
    )
    monkeypatch.setattr(
        "app.instance_config.get_value",
        lambda *args, **kw: (
            "my-bq-proj" if (args and args[-1] == "project") else kw.get("default", "")
        ),
    )


def test_run_sync_fatal_notifies(tmp_path, monkeypatch):
    """An exception inside _run_sync (orchestrator rebuild raises) → the outer
    except handler fires the webhook notifier with the fatal exception."""
    _seed_bq_only_registry(tmp_path)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    _patch_bq_only(monkeypatch)

    from app.api import sync as sync_mod

    # Materialized pass clean; orchestrator rebuild blows up → fatal path.
    monkeypatch.setattr(
        "app.api.sync._run_materialized_pass",
        lambda _c, _b, *, tables=None, source_type=None: {
            "materialized": ["m1"],
            "skipped": [],
            "errors": [],
        },
    )

    class _OrchBoom:
        def rebuild(self):
            raise RuntimeError("orchestrator exploded")

    monkeypatch.setattr(
        "src.orchestrator.SyncOrchestrator", lambda *a, **kw: _OrchBoom()
    )

    captured = {}

    def _spy_notify(*, failed_tables, fatal):
        captured["failed_tables"] = failed_tables
        captured["fatal"] = fatal

    monkeypatch.setattr("app.services.sync_notifier.notify_sync_failure", _spy_notify)

    sync_mod._run_sync()

    assert "fatal" in captured, "notifier must be called on the fatal path"
    assert isinstance(captured["fatal"], RuntimeError)
    assert "orchestrator exploded" in str(captured["fatal"])


def test_run_sync_per_table_errors_notifies(tmp_path, monkeypatch):
    """Non-empty mat_summary['errors'] → notifier called listing the failed
    tables, even when no fatal exception occurred."""
    _seed_bq_only_registry(tmp_path)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    _patch_bq_only(monkeypatch)

    from app.api import sync as sync_mod

    monkeypatch.setattr(
        "app.api.sync._run_materialized_pass",
        lambda _c, _b, *, tables=None, source_type=None: {
            "materialized": [],
            "skipped": [],
            "errors": [{"table": "m1", "error": "budget exceeded"}],
        },
    )

    class _OrchStub:
        def rebuild(self):
            return {}

    monkeypatch.setattr(
        "src.orchestrator.SyncOrchestrator", lambda *a, **kw: _OrchStub()
    )

    captured = {}

    def _spy_notify(*, failed_tables, fatal):
        captured["failed_tables"] = failed_tables
        captured["fatal"] = fatal

    monkeypatch.setattr("app.services.sync_notifier.notify_sync_failure", _spy_notify)

    sync_mod._run_sync()

    assert captured.get("fatal") is None
    assert captured.get("failed_tables") == [
        {"table": "m1", "error": "budget exceeded"}
    ]


def test_run_sync_clean_does_not_notify(tmp_path, monkeypatch):
    """No fatal, no per-table errors → notifier is never called."""
    _seed_bq_only_registry(tmp_path)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    _patch_bq_only(monkeypatch)

    from app.api import sync as sync_mod

    monkeypatch.setattr(
        "app.api.sync._run_materialized_pass",
        lambda _c, _b, *, tables=None, source_type=None: {
            "materialized": ["m1"],
            "skipped": [],
            "errors": [],
        },
    )

    class _OrchStub:
        def rebuild(self):
            return {}

    monkeypatch.setattr(
        "src.orchestrator.SyncOrchestrator", lambda *a, **kw: _OrchStub()
    )

    called = {"n": 0}

    def _spy_notify(**kw):
        called["n"] += 1

    monkeypatch.setattr("app.services.sync_notifier.notify_sync_failure", _spy_notify)

    sync_mod._run_sync()
    assert called["n"] == 0


def test_run_sync_notifier_raising_does_not_break_sync(tmp_path, monkeypatch):
    """If the notifier itself raises (e.g. webhook bug), _run_sync must still
    complete — the notifier hook is best-effort and wrapped defensively."""
    _seed_bq_only_registry(tmp_path)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    _patch_bq_only(monkeypatch)

    from app.api import sync as sync_mod

    monkeypatch.setattr(
        "app.api.sync._run_materialized_pass",
        lambda _c, _b, *, tables=None, source_type=None: {
            "materialized": [],
            "skipped": [],
            "errors": [{"table": "m1", "error": "boom"}],
        },
    )

    rebuilt = {"n": 0}

    class _OrchStub:
        def rebuild(self):
            rebuilt["n"] += 1
            return {}

    monkeypatch.setattr(
        "src.orchestrator.SyncOrchestrator", lambda *a, **kw: _OrchStub()
    )

    def _boom(**kw):
        raise RuntimeError("notifier blew up")

    monkeypatch.setattr("app.services.sync_notifier.notify_sync_failure", _boom)

    # Must not raise; the orchestrator rebuild must still have run.
    sync_mod._run_sync()
    assert rebuilt["n"] == 1
