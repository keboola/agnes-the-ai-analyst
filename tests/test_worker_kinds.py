"""Tests for ``app/worker/kinds.py`` (wave-2B Task 4; ``ducklake-maintenance``
added in wave-2G Task 5 — see ``tests/test_ducklake_maintenance.py`` for its
dedicated coverage; ``analytics-migrate`` added in wave-2G Task 6;
``distribution-mirror`` added in wave-2H Task WF-3 — see
``tests/test_distribution_mirror.py`` for its dedicated coverage).

Verifies:

- ``register_all_kinds()`` registers all five wave-2B job kinds with the
  correct lane (the sixth, ``ducklake-maintenance``, the seventh,
  ``analytics-migrate``, and the eighth, ``distribution-mirror``, are
  asserted alongside them here too — just their presence/lane;
  ``ducklake-maintenance``'s handler behavior lives in
  ``tests/test_ducklake_maintenance.py``, ``analytics-migrate``'s dispatch
  behavior in ``TestAnalyticsMigrateHandler`` below, and
  ``distribution-mirror``'s handler behavior in
  ``tests/test_distribution_mirror.py``).
- Each kind's handler is a thin adapter that DELEGATES to the existing
  function it wraps — no logic is reimplemented here. Verified by
  monkeypatching the wrapped target and asserting it was called (with
  the expected arguments where relevant), not by re-checking the
  wrapped function's own behavior.
- The Jira webhook's incremental-transform path now enqueues a
  ``jira-refresh`` job instead of calling ``SyncOrchestrator().rebuild_source``
  inline.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def clean_job_kinds_registry():
    """The registry is a process-wide module dict — isolate each test."""
    from app.worker.registry import JOB_KINDS

    JOB_KINDS.clear()
    yield
    JOB_KINDS.clear()


@pytest.fixture
def jobs_db(tmp_path, monkeypatch):
    """Fresh system.duckdb under a tmp DATA_DIR (has the ``jobs`` table),
    closed after the test. Mirrors ``tests/test_worker_runtime.py``'s
    ``worker_db`` fixture."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("AGNES_DB_URL", raising=False)
    from src.db import close_system_db, get_system_db

    get_system_db()  # forces schema creation (incl. the jobs table)
    yield
    close_system_db()


class TestRegisterAllKinds:
    def test_registers_eight_kinds(self):
        from app.worker.kinds import register_all_kinds
        from app.worker.registry import JOB_KINDS

        register_all_kinds()

        assert set(JOB_KINDS) == {
            "data-refresh",
            "marketplaces-sync",
            "session-collector",
            "corporate-memory",
            "jira-refresh",
            "ducklake-maintenance",
            "analytics-migrate",
            "distribution-mirror",
        }

    def test_lanes_are_correct(self):
        from app.worker.kinds import register_all_kinds
        from app.worker.registry import HEAVY_LANE, JOB_KINDS, LIGHT_LANE

        register_all_kinds()

        assert JOB_KINDS["data-refresh"].lane == HEAVY_LANE
        assert JOB_KINDS["jira-refresh"].lane == HEAVY_LANE
        assert JOB_KINDS["marketplaces-sync"].lane == LIGHT_LANE
        assert JOB_KINDS["session-collector"].lane == LIGHT_LANE
        assert JOB_KINDS["corporate-memory"].lane == LIGHT_LANE
        assert JOB_KINDS["ducklake-maintenance"].lane == LIGHT_LANE
        assert JOB_KINDS["analytics-migrate"].lane == HEAVY_LANE
        assert JOB_KINDS["distribution-mirror"].lane == LIGHT_LANE

    def test_idempotent_reregistration(self):
        """Calling register_all_kinds() twice (e.g. test re-imports, or a
        future re-init path) must not raise or duplicate entries."""
        from app.worker.kinds import register_all_kinds
        from app.worker.registry import JOB_KINDS

        register_all_kinds()
        register_all_kinds()

        assert len(JOB_KINDS) == 8


class TestDataRefreshHandler:
    def test_delegates_to_run_sync_with_defaults(self, monkeypatch):
        from app.worker.kinds import register_all_kinds
        from app.worker.registry import JOB_KINDS

        register_all_kinds()

        calls = []
        monkeypatch.setattr(
            "app.api.sync._run_sync",
            lambda tables=None, source_type_filter=None: calls.append((tables, source_type_filter)),
        )

        JOB_KINDS["data-refresh"].handler({})

        assert calls == [(None, None)]

    def test_delegates_to_run_sync_with_payload_overrides(self, monkeypatch):
        from app.worker.kinds import register_all_kinds
        from app.worker.registry import JOB_KINDS

        register_all_kinds()

        calls = []
        monkeypatch.setattr(
            "app.api.sync._run_sync",
            lambda tables=None, source_type_filter=None: calls.append((tables, source_type_filter)),
        )

        JOB_KINDS["data-refresh"].handler({"tables": ["orders"], "source": "keboola"})

        assert calls == [(["orders"], "keboola")]

    def test_raises_when_run_sync_reports_failure(self, monkeypatch):
        """Job-outcome honesty (wave-2B review carry-over, W2B-4/7):
        `_run_sync` used to swallow every failure internally and return
        nothing, so a `data-refresh` job always finalized 'done' even when
        the sync itself failed. `_run_sync` now returns `False` on a fatal
        or per-table failure; the handler must turn that into a raised
        exception so the worker's lane-slot records the job `failed`
        (with `retry_in_seconds` from the kind's registration) instead of
        `done`."""
        from app.worker.kinds import register_all_kinds
        from app.worker.registry import JOB_KINDS

        register_all_kinds()
        monkeypatch.setattr("app.api.sync._run_sync", lambda tables=None, source_type_filter=None: False)

        with pytest.raises(RuntimeError):
            JOB_KINDS["data-refresh"].handler({})

    @pytest.mark.parametrize("run_sync_result", [True, None])
    def test_does_not_raise_when_run_sync_succeeds_or_noops(self, monkeypatch, run_sync_result):
        """`True` (clean run) and `None` (this call was a no-op — another
        same-process `_run_sync` already held `_sync_lock`) must both be
        treated as "not a failure of THIS job" — only `False` raises."""
        from app.worker.kinds import register_all_kinds
        from app.worker.registry import JOB_KINDS

        register_all_kinds()
        monkeypatch.setattr("app.api.sync._run_sync", lambda tables=None, source_type_filter=None: run_sync_result)

        JOB_KINDS["data-refresh"].handler({})  # must not raise


class TestMarketplacesSyncHandler:
    def test_delegates_to_sync_marketplaces(self, monkeypatch):
        from app.worker.kinds import register_all_kinds
        from app.worker.registry import JOB_KINDS

        register_all_kinds()

        calls = []
        monkeypatch.setattr("src.marketplace.sync_marketplaces", lambda: calls.append(True) or {"synced": []})

        JOB_KINDS["marketplaces-sync"].handler({})

        assert calls == [True]


class TestSessionCollectorHandler:
    def test_delegates_to_collector_run(self, monkeypatch):
        from app.worker.kinds import register_all_kinds
        from app.worker.registry import JOB_KINDS

        register_all_kinds()

        calls = []

        def fake_run(dry_run=False, verbose=False):
            calls.append((dry_run, verbose))
            return (0, {})

        monkeypatch.setattr("services.session_collector.collector.run", fake_run)

        JOB_KINDS["session-collector"].handler({})

        assert calls == [(False, False)]


class TestCorporateMemoryHandler:
    def test_delegates_to_collect_all(self, monkeypatch):
        from app.worker.kinds import register_all_kinds
        from app.worker.registry import JOB_KINDS

        register_all_kinds()

        calls = []
        monkeypatch.setattr(
            "services.corporate_memory.collector.collect_all",
            lambda dry_run=False: calls.append(dry_run) or {},
        )

        JOB_KINDS["corporate-memory"].handler({})

        assert calls == [False]


class TestJiraRefreshHandler:
    def test_delegates_to_orchestrator_rebuild_source(self, monkeypatch):
        from app.worker.kinds import register_all_kinds
        from app.worker.registry import JOB_KINDS

        register_all_kinds()

        calls = []

        class FakeOrchestrator:
            def rebuild_source(self, name):
                calls.append(name)
                return {}

        monkeypatch.setattr("src.orchestrator.SyncOrchestrator", FakeOrchestrator)

        JOB_KINDS["jira-refresh"].handler({})

        assert calls == ["jira"]


class TestAnalyticsMigrateHandler:
    """``analytics-migrate`` (wave-2G Task 6) — a thin adapter over
    ``SyncOrchestrator().migrate_to_backend(to)``, dispatch-only (the
    method's own behavior is covered in
    ``tests/test_orchestrator.py::TestMigrateToBackend`` and
    ``tests/test_orchestrator_ducklake.py::TestMigrateToBackendDucklakeDirection``)."""

    def test_delegates_to_migrate_to_backend_with_payload_target(self, monkeypatch):
        from app.worker.kinds import register_all_kinds
        from app.worker.registry import JOB_KINDS

        register_all_kinds()

        calls = []

        class FakeOrchestrator:
            def migrate_to_backend(self, to):
                calls.append(to)
                return {}

        monkeypatch.setattr("src.orchestrator.SyncOrchestrator", FakeOrchestrator)

        JOB_KINDS["analytics-migrate"].handler({"to": "ducklake"})

        assert calls == ["ducklake"]

    def test_propagates_invalid_target_error(self, monkeypatch):
        """An unknown ``to`` value re-raises ``migrate_to_backend``'s own
        ``ValueError`` — the worker's lane-slot handler turns that into a
        failed job the same way any other handler exception does."""
        from app.worker.kinds import register_all_kinds
        from app.worker.registry import JOB_KINDS

        register_all_kinds()

        with pytest.raises(ValueError):
            JOB_KINDS["analytics-migrate"].handler({"to": "bogus"})


class TestJiraWebhookEnqueues:
    """The Jira incremental-transform path must enqueue a ``jira-refresh``
    job instead of calling ``SyncOrchestrator().rebuild_source`` inline.
    """

    def test_trigger_incremental_transform_enqueues_not_inline(self, jobs_db, monkeypatch):
        from connectors.jira.service import trigger_incremental_transform

        # Fail loudly if anything still calls the orchestrator inline.
        class ExplodingOrchestrator:
            def rebuild_source(self, name):  # pragma: no cover - must not be hit
                raise AssertionError("SyncOrchestrator().rebuild_source called inline; expected enqueue instead")

        monkeypatch.setattr("src.orchestrator.SyncOrchestrator", ExplodingOrchestrator)
        monkeypatch.setattr(
            "connectors.jira.incremental_transform.transform_single_issue",
            lambda issue_key, deleted=False: True,
        )

        result = trigger_incremental_transform("KSP-1", deleted=False)

        assert result is True

        from src.repositories import jobs_repo

        rows = jobs_repo().list(kind="jira-refresh")
        assert len(rows) == 1
        assert rows[0]["idempotency_key"] == "jira-refresh"

    def test_second_webhook_dedups_via_idempotency_key(self, jobs_db, monkeypatch):
        """Two webhook events before the job runs must not queue two
        rebuilds — enqueue()'s idempotency dedup collapses them."""
        from connectors.jira.service import trigger_incremental_transform

        monkeypatch.setattr("src.orchestrator.SyncOrchestrator", lambda: None)
        monkeypatch.setattr(
            "connectors.jira.incremental_transform.transform_single_issue",
            lambda issue_key, deleted=False: True,
        )

        trigger_incremental_transform("KSP-1", deleted=False)
        trigger_incremental_transform("KSP-2", deleted=False)

        from src.repositories import jobs_repo

        rows = jobs_repo().list(kind="jira-refresh")
        assert len(rows) == 1

    def test_webhook_during_running_refresh_enqueues_coalescing_followup(self, jobs_db, monkeypatch):
        """A webhook whose parquet write lands while a jira-refresh job is
        already RUNNING must not be dropped: that running job may have
        started (and read parquet) before this write, so the dedup above
        (which matches 'queued' or 'running') would otherwise silently
        swallow it. A follow-up job (distinct idempotency key) must be
        queued to guarantee a rebuild strictly after this write."""
        from connectors.jira.service import trigger_incremental_transform
        from src.repositories import jobs_repo

        monkeypatch.setattr("src.orchestrator.SyncOrchestrator", lambda: None)
        monkeypatch.setattr(
            "connectors.jira.incremental_transform.transform_single_issue",
            lambda issue_key, deleted=False: True,
        )

        # Simulate a jira-refresh job already RUNNING (e.g. claimed by the
        # worker before this webhook's parquet write landed).
        jobs_repo().enqueue("jira-refresh", {}, idempotency_key="jira-refresh")
        claimed = jobs_repo().claim_next(kinds=["jira-refresh"], worker_id="test-worker")
        assert claimed is not None and claimed["status"] == "running"

        trigger_incremental_transform("KSP-1", deleted=False)

        rows = jobs_repo().list(kind="jira-refresh")
        assert len(rows) == 2
        by_key = {r["idempotency_key"]: r for r in rows}
        assert by_key["jira-refresh"]["status"] == "running"
        assert by_key["jira-refresh-followup"]["status"] == "queued"

    def test_second_webhook_mid_run_dedups_onto_followup(self, jobs_db, monkeypatch):
        """A second webhook while the primary is still running must dedup
        onto the same follow-up row, not create a third."""
        from connectors.jira.service import trigger_incremental_transform
        from src.repositories import jobs_repo

        monkeypatch.setattr("src.orchestrator.SyncOrchestrator", lambda: None)
        monkeypatch.setattr(
            "connectors.jira.incremental_transform.transform_single_issue",
            lambda issue_key, deleted=False: True,
        )

        jobs_repo().enqueue("jira-refresh", {}, idempotency_key="jira-refresh")
        jobs_repo().claim_next(kinds=["jira-refresh"], worker_id="test-worker")

        trigger_incremental_transform("KSP-1", deleted=False)
        trigger_incremental_transform("KSP-2", deleted=False)

        rows = jobs_repo().list(kind="jira-refresh")
        assert len(rows) == 2  # still exactly 1 running + 1 queued follow-up
