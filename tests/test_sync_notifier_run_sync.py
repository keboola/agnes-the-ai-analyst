"""Integration: app.api.sync._run_sync wires the webhook notifier on failure.

Two failure surfaces are covered:
  - the outer ``except`` (fatal path) — one webhook POST naming the exception;
  - non-empty ``mat_summary['errors']`` (per-table errors) — POST lists them.

And the negatives: unset URL → no POST; a webhook that raises → sync still
completes (best-effort).
"""

import duckdb

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
    monkeypatch.setattr("app.instance_config.get_data_source_type", lambda: "bigquery")
    monkeypatch.setattr(
        "app.instance_config.get_value",
        lambda *args, **kw: "my-bq-proj" if (args and args[-1] == "project") else kw.get("default", ""),
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

    monkeypatch.setattr("src.orchestrator.SyncOrchestrator", lambda *a, **kw: _OrchBoom())

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

    monkeypatch.setattr("src.orchestrator.SyncOrchestrator", lambda *a, **kw: _OrchStub())

    captured = {}

    def _spy_notify(*, failed_tables, fatal):
        captured["failed_tables"] = failed_tables
        captured["fatal"] = fatal

    monkeypatch.setattr("app.services.sync_notifier.notify_sync_failure", _spy_notify)

    sync_mod._run_sync()

    assert captured.get("fatal") is None
    assert captured.get("failed_tables") == [{"table": "m1", "error": "budget exceeded"}]


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

    monkeypatch.setattr("src.orchestrator.SyncOrchestrator", lambda *a, **kw: _OrchStub())

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

    monkeypatch.setattr("src.orchestrator.SyncOrchestrator", lambda *a, **kw: _OrchStub())

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

    monkeypatch.setattr("src.orchestrator.SyncOrchestrator", lambda *a, **kw: _OrchTimeout())

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
        orch_mod,
        "SyncOrchestrator",
        lambda *a, **kw: MagicMock(rebuild=MagicMock(return_value={})),
        raising=False,
    )

    monkeypatch.setenv("KEBOOLA_STORAGE_TOKEN", "test-token")
    monkeypatch.setenv("KEBOOLA_STACK_URL", "https://test.example")

    from src.repositories.table_registry import TableRegistryRepository

    monkeypatch.setattr(
        TableRegistryRepository,
        "list_local",
        lambda self, *a, **kw: [
            {
                "id": "x",
                "name": "x",
                "source_type": "keboola",
                "bucket": "in.c-x",
                "source_table": "y",
                "query_mode": "local",
            }
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

    monkeypatch.setattr("src.orchestrator.SyncOrchestrator", lambda *a, **kw: _OrchBoom())

    calls = []

    def _spy_notify(*, failed_tables, fatal):
        calls.append({"failed_tables": failed_tables, "fatal": fatal})

    monkeypatch.setattr("app.services.sync_notifier.notify_sync_failure", _spy_notify)

    sync_mod._run_sync()

    assert len(calls) == 1, f"expected a single combined alert, got {len(calls)}"
    assert isinstance(calls[0]["fatal"], RuntimeError)
    # The combined alert still carries the per-table errors collected earlier.
    assert calls[0]["failed_tables"] == [{"table": "m1", "error": "budget exceeded"}]


def test_run_sync_success_notifies_completed(tmp_path, monkeypatch):
    """A successful run fires notify_sync_completed with THIS run's synced
    tables only — narrowed from the orchestrator's full rebuild result,
    which can carry tables from prior, unrelated runs (#412 review: passing
    the raw rebuild result spams a notification on every tick)."""
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
            # "stale_table" was synced in a PRIOR run and is still
            # re-attached by every rebuild — it must not show up as
            # "refreshed" in THIS run's notification.
            return {"bigquery": ["m1", "stale_table"]}

    monkeypatch.setattr("src.orchestrator.SyncOrchestrator", lambda *a, **kw: _OrchStub())

    captured = {}

    def _spy_notify(synced, *, error_count):
        captured["synced"] = synced
        captured["error_count"] = error_count

    monkeypatch.setattr("app.services.sync_notifier.notify_sync_completed", _spy_notify)

    sync_mod._run_sync()

    assert captured.get("synced") == {"bigquery": ["m1"]}
    assert captured.get("error_count") == 0


def test_run_sync_partial_errors_notify_completed_with_error_count(tmp_path, monkeypatch):
    """A run with one per-table failure that still synced another table →
    the completed event fires AFTER the error accounting, carrying the
    run's error count (so the client sees 'partial', not an unqualified
    success) and ONLY the table(s) that actually succeeded this run — while
    the job path still reports the run as failed."""
    _seed_bq_only_registry(tmp_path)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    _patch_bq_only(monkeypatch)

    from app.api import sync as sync_mod

    monkeypatch.setattr(
        "app.api.sync._run_materialized_pass",
        lambda _c, _b, *, tables=None, source_type=None: {
            "materialized": ["m1"],
            "skipped": [],
            "errors": [{"table": "m2", "error": "budget exceeded"}],
        },
    )

    class _OrchStub:
        def rebuild(self):
            return {"bigquery": ["m1", "m2", "other_table"]}

    monkeypatch.setattr("src.orchestrator.SyncOrchestrator", lambda *a, **kw: _OrchStub())

    captured = {}

    def _spy_notify(synced, *, error_count):
        captured["synced"] = synced
        captured["error_count"] = error_count

    monkeypatch.setattr("app.services.sync_notifier.notify_sync_completed", _spy_notify)

    assert sync_mod._run_sync() is False
    assert captured.get("synced") == {"bigquery": ["m1"]}
    assert captured.get("error_count") == 1


def test_run_sync_nothing_due_no_completed_notification(tmp_path, monkeypatch):
    """A scheduled tick where nothing was due to sync must NOT fire a desktop
    notification, even though the orchestrator's rebuild still re-attaches
    every table from prior runs (#412 review: the rebuild result is not
    "what changed this tick" — the earlier bug fired a "tables refreshed"
    alert on every tick regardless of whether anything actually synced)."""
    _seed_bq_only_registry(tmp_path)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    _patch_bq_only(monkeypatch)

    from app.api import sync as sync_mod

    # Nothing due this tick — the materialized pass skips its only row.
    monkeypatch.setattr(
        "app.api.sync._run_materialized_pass",
        lambda _c, _b, *, tables=None, source_type=None: {
            "materialized": [],
            "skipped": [{"table": "m1", "reason": "due_check"}],
            "errors": [],
        },
    )

    class _OrchStub:
        def rebuild(self):
            # Full rebuild still reports the table from a PRIOR run —
            # exactly what must not reach the notifier as "refreshed".
            return {"bigquery": ["m1"]}

    monkeypatch.setattr("src.orchestrator.SyncOrchestrator", lambda *a, **kw: _OrchStub())

    calls = []
    monkeypatch.setattr(
        "app.services.sync_notifier.users_repo",
        lambda: type("R", (), {"list_all": staticmethod(lambda: [{"id": "alice", "active": True}])})(),
    )
    monkeypatch.setattr(
        "app.services.sync_notifier.publish_notification",
        lambda user, payload: calls.append((user, payload)),
    )

    sync_mod._run_sync()

    assert calls == [], "nothing was due this tick — no desktop notification should fire"


def test_run_sync_two_synced_tables_fire_one_event_with_count(tmp_path, monkeypatch):
    """A run that syncs 2 tables of the same source fires exactly one
    sync_completed event for that source with table_count=2 — the
    orchestrator's full rebuild result (which can carry an unrelated,
    previously-synced table) must not inflate that count."""
    _seed_bq_only_registry(tmp_path)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    _patch_bq_only(monkeypatch)

    from app.api import sync as sync_mod

    monkeypatch.setattr(
        "app.api.sync._run_materialized_pass",
        lambda _c, _b, *, tables=None, source_type=None: {
            "materialized": ["m1", "m2"],
            "skipped": [],
            "errors": [],
        },
    )

    class _OrchStub:
        def rebuild(self):
            return {"bigquery": ["m1", "m2", "old_table"]}

    monkeypatch.setattr("src.orchestrator.SyncOrchestrator", lambda *a, **kw: _OrchStub())

    calls = []
    monkeypatch.setattr(
        "app.services.sync_notifier.users_repo",
        lambda: type("R", (), {"list_all": staticmethod(lambda: [{"id": "alice", "active": True}])})(),
    )
    monkeypatch.setattr(
        "app.services.sync_notifier.publish_notification",
        lambda user, payload: calls.append((user, payload)),
    )

    sync_mod._run_sync()

    assert len(calls) == 1, f"expected exactly one sync_completed event, got {len(calls)}"
    user_id, payload = calls[0]
    assert user_id == "alice"
    # The envelope/outcome contract (kind/title/message/status/error_count)
    # stays intact — only the scoping (source + table_count) is this fix's
    # concern.
    assert payload["kind"] == "sync_completed"
    assert payload["title"] == "Sync completed"
    assert payload["status"] == "ok"
    assert payload["error_count"] == 0
    assert payload["source"] == "bigquery"
    assert payload["table_count"] == 2
    assert "old_table" not in payload["message"]


def _stub_extractor_subprocess(monkeypatch, *, stdout: str, returncode: int):
    """Replace the Keboola extractor subprocess with a canned stdout/exit code.

    `_run_sync` reads the extractor's per-table stats back out of its stdout
    line and its overall verdict out of the exit code, so those two values are
    the whole contract this stub needs to reproduce.
    """
    import subprocess

    class _Popen:
        def __init__(self, cmd, **kwargs):
            self.pid = 4242
            self.returncode = returncode

        def communicate(self, input=None, timeout=None):
            return (stdout, "")

    monkeypatch.setattr(subprocess, "Popen", _Popen)


def _keboola_env(tmp_path, monkeypatch, views):
    """Keboola instance with a stubbed orchestrator rebuild + materialized pass.

    Returns the `captured` dict a `notify_sync_completed` spy writes into.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KEBOOLA_STORAGE_TOKEN", "test-token")
    monkeypatch.setenv("KEBOOLA_STACK_URL", "https://test.example")

    from app import instance_config as ic_mod

    monkeypatch.setattr(ic_mod, "get_data_source_type", lambda: "keboola")
    monkeypatch.setattr(ic_mod, "get_value", lambda *a, **kw: kw.get("default", ""))

    monkeypatch.setattr(
        "app.api.sync._run_materialized_pass",
        lambda _c, _b, *, tables=None, source_type=None: {
            "materialized": [],
            "skipped": [{"table": "mat1", "reason": "due_check"}],
            "errors": [],
        },
    )

    class _OrchStub:
        def rebuild(self):
            return dict(views)

    monkeypatch.setattr("src.orchestrator.SyncOrchestrator", lambda *a, **kw: _OrchStub())

    captured = {}

    def _spy_notify(synced, *, error_count):
        captured["synced"] = synced
        captured["error_count"] = error_count

    monkeypatch.setattr("app.services.sync_notifier.notify_sync_completed", _spy_notify)
    return captured


def test_scoped_trigger_does_not_claim_rows_the_extractor_never_wrote(tmp_path, monkeypatch):
    """A `tables=[...]` operator trigger reads registry rows directly, so
    `table_configs` can carry materialized and remote rows. The Keboola
    extractor silently `continue`s over materialized rows (the materialized
    pass owns them — and here it skipped this one on its due check) and only
    creates a view for remote rows (no data is downloaded; `agnes pull` skips
    them entirely). Neither was refreshed, so neither may be announced as such.
    """
    captured = _keboola_env(tmp_path, monkeypatch, {"keboola": ["mat1", "rem1"]})

    from src.repositories import table_registry_repo

    repo = table_registry_repo()
    repo.register(
        id="mat1",
        name="mat1",
        source_type="keboola",
        query_mode="materialized",
        source_query="SELECT 1",
    )
    repo.register(
        id="rem1",
        name="rem1",
        source_type="keboola",
        query_mode="remote",
        bucket="in.c-x",
        source_table="rem1",
    )

    # Extractor exit 0: it skipped mat1 outright and counted rem1's view as
    # "extracted" — no per-table error for either.
    _stub_extractor_subprocess(
        monkeypatch,
        stdout='{"tables_extracted": 1, "tables_failed": 0, "errors": []}',
        returncode=0,
    )

    from app.api import sync as sync_mod

    sync_mod._run_sync(tables=["mat1", "rem1"])

    assert captured.get("synced") == {}, "neither the skipped materialized row nor the remote view landed data this run"


def test_partial_extractor_without_recovered_stats_claims_nothing(tmp_path, monkeypatch):
    """Exit 2 means SOME table failed. When the stats line can't be parsed
    (the code already anticipates that — it falls back to a placeholder error),
    there is no per-table error list to subtract, so the pre-fix code announced
    every attempted table as refreshed, failures included. Without evidence of
    which tables survived, the run must claim none.
    """
    captured = _keboola_env(tmp_path, monkeypatch, {"keboola": ["t1", "t2"]})

    from src.repositories import table_registry_repo

    repo = table_registry_repo()
    for tid in ("t1", "t2"):
        repo.register(
            id=tid,
            name=tid,
            source_type="keboola",
            query_mode="local",
            bucket="in.c-x",
            source_table=tid,
        )

    _stub_extractor_subprocess(
        monkeypatch,
        stdout="Traceback (most recent call last): boom",
        returncode=2,
    )

    from app.api import sync as sync_mod

    sync_mod._run_sync(tables=["t1", "t2"])

    assert captured.get("synced") == {}, "a partial run with unrecoverable stats knows nothing about survivors"
    assert captured.get("error_count") == 1


def test_partial_extractor_with_recovered_stats_reports_the_survivor(tmp_path, monkeypatch):
    """The narrowing above must not swing the other way: when the per-table
    errors WERE recovered, the tables that did not fail are genuine refreshes
    and must still reach the notification.
    """
    captured = _keboola_env(tmp_path, monkeypatch, {"keboola": ["t1", "t2"]})

    from src.repositories import table_registry_repo

    repo = table_registry_repo()
    for tid in ("t1", "t2"):
        repo.register(
            id=tid,
            name=tid,
            source_type="keboola",
            query_mode="local",
            bucket="in.c-x",
            source_table=tid,
        )

    _stub_extractor_subprocess(
        monkeypatch,
        stdout='{"tables_extracted": 1, "tables_failed": 1, "errors": [{"table": "t2", "error": "export failed"}]}',
        returncode=2,
    )

    from app.api import sync as sync_mod

    sync_mod._run_sync(tables=["t1", "t2"])

    assert captured.get("synced") == {"keboola": ["t1"]}
    assert captured.get("error_count") == 1


def test_run_sync_notify_completed_raising_does_not_break_sync(tmp_path, monkeypatch):
    """notify_sync_completed is best-effort — a raise must not fail the sync."""
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
            return {"bigquery": ["m1"]}

    monkeypatch.setattr("src.orchestrator.SyncOrchestrator", lambda *a, **kw: _OrchStub())

    def _boom(views, **kw):
        raise RuntimeError("notifier blew up")

    monkeypatch.setattr("app.services.sync_notifier.notify_sync_completed", _boom)

    # Must not raise, and the run must still report success.
    assert sync_mod._run_sync() is True
