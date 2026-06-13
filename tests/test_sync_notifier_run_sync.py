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


def test_run_sync_timeout_notifies(tmp_path, monkeypatch):
    """#648 review: a subprocess.TimeoutExpired reaching the OUTER handler
    (its own except branch, more specific than `except Exception`) must still
    fire the webhook notifier — a swallowed timeout is exactly the silent
    failure this feature exists to surface."""
    import subprocess

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

    class _OrchTimeout:
        def rebuild(self):
            raise subprocess.TimeoutExpired(cmd="extractor", timeout=600)

    monkeypatch.setattr(
        "src.orchestrator.SyncOrchestrator", lambda *a, **kw: _OrchTimeout()
    )

    captured = {}

    def _spy_notify(*, failed_tables, fatal):
        captured["failed_tables"] = failed_tables
        captured["fatal"] = fatal

    monkeypatch.setattr("app.services.sync_notifier.notify_sync_failure", _spy_notify)

    sync_mod._run_sync()

    assert "fatal" in captured, "notifier must be called on the timeout path"
    assert isinstance(captured["fatal"], subprocess.TimeoutExpired)


def test_run_sync_extractor_timeout_notifies(tmp_path, monkeypatch, capsys):
    """#648 review: the Keboola extractor's LOCAL timeout catch sets
    result=None and skips exit-code error collection, yet a stalled
    extractor must still raise a per-table webhook alert — so the timeout
    now appends to collected_errors and the end-of-try notifier fires."""
    import subprocess
    from unittest.mock import MagicMock

    from app.api import sync as sync_mod

    class _TimeoutPopen:
        def __init__(self, cmd, **kwargs):
            self.pid = 999
            self.returncode = None
            self._calls = 0

        def communicate(self, input=None, timeout=None):
            self._calls += 1
            if self._calls == 1:
                raise subprocess.TimeoutExpired(cmd="extractor", timeout=timeout)
            return ("", "")

    monkeypatch.setattr(subprocess, "Popen", _TimeoutPopen)
    monkeypatch.setattr(sync_mod.os, "killpg", lambda *a, **k: None)

    from src import orchestrator as orch_mod
    monkeypatch.setattr(
        orch_mod, "SyncOrchestrator",
        lambda *a, **kw: MagicMock(rebuild=MagicMock(return_value={})),
        raising=False,
    )

    monkeypatch.setenv("KEBOOLA_STORAGE_TOKEN", "test-token")
    monkeypatch.setenv("KEBOOLA_STACK_URL", "https://test.example")

    from src.repositories.table_registry import TableRegistryRepository
    monkeypatch.setattr(
        TableRegistryRepository, "list_local",
        lambda self, *a, **kw: [
            {"id": "x", "name": "x", "source_type": "keboola",
             "bucket": "in.c-x", "source_table": "y", "query_mode": "local"}
        ],
    )

    fake_conn = MagicMock()
    from src import db as db_mod
    from app import instance_config as ic_mod
    monkeypatch.setattr(db_mod, "get_system_db", lambda: fake_conn)
    monkeypatch.setattr(ic_mod, "get_data_source_type", lambda: "keboola")
    monkeypatch.setattr(ic_mod, "get_value", lambda *a, **kw: "")

    captured = {}

    def _spy_notify(*, failed_tables, fatal):
        captured["failed_tables"] = failed_tables
        captured["fatal"] = fatal

    monkeypatch.setattr("app.services.sync_notifier.notify_sync_failure", _spy_notify)

    sync_mod._run_sync()

    assert "failed_tables" in captured, "extractor timeout must trigger the notifier"
    assert captured["fatal"] is None
    assert any("timed out" in e["error"] for e in captured["failed_tables"])


def test_run_sync_per_table_then_fatal_notifies_once(tmp_path, monkeypatch):
    """#648 review: per-table errors from the materialized pass + a later
    fatal crash (orchestrator rebuild) must produce ONE combined webhook
    alert (the fatal path), not a per-table POST followed by an overlapping
    fatal POST for the same run."""
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

    class _OrchBoom:
        def rebuild(self):
            raise RuntimeError("rebuild exploded")

    monkeypatch.setattr(
        "src.orchestrator.SyncOrchestrator", lambda *a, **kw: _OrchBoom()
    )

    calls = []

    def _spy_notify(*, failed_tables, fatal):
        calls.append({"failed_tables": failed_tables, "fatal": fatal})

    monkeypatch.setattr("app.services.sync_notifier.notify_sync_failure", _spy_notify)

    sync_mod._run_sync()

    assert len(calls) == 1, f"expected a single combined alert, got {len(calls)}"
    assert isinstance(calls[0]["fatal"], RuntimeError)
    # The combined alert still carries the per-table errors collected earlier.
    assert calls[0]["failed_tables"] == [{"table": "m1", "error": "budget exceeded"}]
