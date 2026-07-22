"""Three-plane §3.1: the api plane must not write analytics in-process.

On a role-split deployment (process without the worker role), the three
in-api analytics writers convert to enqueue-and-ack:

  - admin BQ post-register/update rebuild  → ``analytics-rebuild`` job
  - collections derived-table purge        → ``collections-purge`` job

On a single-box ``all`` deployment (worker role enabled in-process) the
existing synchronous / BackgroundTask behavior is unchanged — zero UX
change for today's deployments.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


class _SpyJobs:
    def __init__(self):
        self.enqueued = []

    def enqueue(self, kind, payload=None, idempotency_key=None, **kw):
        self.enqueued.append({"kind": kind, "payload": payload, "idempotency_key": idempotency_key})
        return {"id": "job-1", "deduped": False}


# ---- job kinds exist + dispatch ---------------------------------------------


def test_new_kinds_registered():
    from app.worker.kinds import register_all_kinds
    from app.worker.registry import JOB_KINDS

    register_all_kinds()
    for name in ("analytics-rebuild", "collections-purge"):
        kind = JOB_KINDS.get(name)
        assert kind is not None, f"{name} must be a registered job kind"
        assert kind.lane == "heavy"


def test_analytics_rebuild_handler_runs_materialize(monkeypatch):
    from app.worker import kinds as kinds_mod

    called = {}

    def _fake_materialize():
        called["ran"] = True
        return {"errors": []}

    monkeypatch.setattr("app.api.admin._materialize_bigquery_extract", _fake_materialize)
    kinds_mod._run_analytics_rebuild({})
    assert called.get("ran") is True


def test_analytics_rebuild_handler_raises_on_errors(monkeypatch):
    from app.worker import kinds as kinds_mod

    monkeypatch.setattr(
        "app.api.admin._materialize_bigquery_extract",
        lambda: {"errors": [{"table": "t", "error": "boom"}]},
    )
    with pytest.raises(RuntimeError):
        kinds_mod._run_analytics_rebuild({})


def test_collections_purge_handler_dispatches(monkeypatch):
    from app.worker import kinds as kinds_mod

    calls = []
    monkeypatch.setattr(
        "app.api.collections._purge_derived_tabular_rows",
        lambda corpus_id: calls.append(("corpus", corpus_id)),
    )
    monkeypatch.setattr(
        "app.api.collections._purge_derived_tabular_row_for_file",
        lambda corpus_id, file_id: calls.append(("file", corpus_id, file_id)),
    )
    kinds_mod._run_collections_purge({"corpus_id": "c1"})
    kinds_mod._run_collections_purge({"corpus_id": "c1", "file_id": "f1"})
    assert calls == [("corpus", "c1"), ("file", "c1", "f1")]


# ---- admin: BQ materialize routing -------------------------------------------


def test_admin_schedule_uses_background_task_when_worker_role(monkeypatch):
    from app.api import admin as admin_mod

    monkeypatch.setattr("app.roles.role_enabled", lambda r: True)
    spy = _SpyJobs()
    monkeypatch.setattr("src.repositories.jobs_repo", lambda: spy)
    background = MagicMock()

    enqueued = admin_mod._schedule_bq_materialize(background)

    assert enqueued is False
    background.add_task.assert_called_once()
    assert spy.enqueued == []


def test_admin_schedule_enqueues_job_when_api_only(monkeypatch):
    from app.api import admin as admin_mod
    from app.roles import Role

    monkeypatch.setattr("app.roles.role_enabled", lambda r: r != Role.WORKER)
    spy = _SpyJobs()
    monkeypatch.setattr("src.repositories.jobs_repo", lambda: spy)
    background = MagicMock()

    enqueued = admin_mod._schedule_bq_materialize(background)

    assert enqueued is True
    background.add_task.assert_not_called()
    assert [e["kind"] for e in spy.enqueued] == ["analytics-rebuild"]


def test_admin_timeout_runner_enqueues_when_api_only(monkeypatch):
    """_run_bigquery_materialize_with_timeout must not touch analytics on an
    api-only replica — it enqueues and reports status='enqueued' so the
    endpoints can 202."""
    from app.api import admin as admin_mod
    from app.roles import Role

    monkeypatch.setattr("app.roles.role_enabled", lambda r: r != Role.WORKER)
    spy = _SpyJobs()
    monkeypatch.setattr("src.repositories.jobs_repo", lambda: spy)
    ran = {}
    monkeypatch.setattr(
        "app.api.admin._materialize_bigquery_extract",
        lambda: ran.setdefault("ran", True) or {"errors": []},
    )

    outcome = admin_mod._run_bigquery_materialize_with_timeout(MagicMock())

    assert outcome["status"] == "enqueued"
    assert ran == {}, "api-only replica must not run the rebuild in-process"
    assert [e["kind"] for e in spy.enqueued] == ["analytics-rebuild"]


# ---- collections: purge routing ----------------------------------------------


def test_collections_purge_inline_when_worker_role(monkeypatch):
    from app.api import collections as col_mod

    monkeypatch.setattr("app.roles.role_enabled", lambda r: True)
    spy = _SpyJobs()
    monkeypatch.setattr("src.repositories.jobs_repo", lambda: spy)
    calls = []
    monkeypatch.setattr(col_mod, "_purge_derived_tabular_rows", lambda c: calls.append(("corpus", c)))
    monkeypatch.setattr(col_mod, "_purge_derived_tabular_row_for_file", lambda c, f: calls.append(("file", c, f)))

    col_mod._schedule_derived_purge("c1")
    col_mod._schedule_derived_purge("c1", "f1")

    assert calls == [("corpus", "c1"), ("file", "c1", "f1")]
    assert spy.enqueued == []


def test_collections_purge_enqueues_when_api_only(monkeypatch):
    from app.api import collections as col_mod
    from app.roles import Role

    monkeypatch.setattr("app.roles.role_enabled", lambda r: r != Role.WORKER)
    spy = _SpyJobs()
    monkeypatch.setattr("src.repositories.jobs_repo", lambda: spy)
    calls = []
    monkeypatch.setattr(col_mod, "_purge_derived_tabular_rows", lambda c: calls.append(c))
    monkeypatch.setattr(col_mod, "_purge_derived_tabular_row_for_file", lambda c, f: calls.append((c, f)))

    col_mod._schedule_derived_purge("c1")
    col_mod._schedule_derived_purge("c1", "f1")

    assert calls == [], "api-only replica must not purge in-process"
    assert [e["kind"] for e in spy.enqueued] == ["collections-purge", "collections-purge"]
    assert spy.enqueued[0]["payload"] == {"corpus_id": "c1", "file_id": None}
    assert spy.enqueued[1]["payload"] == {"corpus_id": "c1", "file_id": "f1"}
