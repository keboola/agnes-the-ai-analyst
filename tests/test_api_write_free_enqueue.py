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


def test_collections_purge_handler_reingests_after_purge_in_order(monkeypatch):
    """Regression guard: a reingest on a role-split deployment must purge THEN
    ingest, in that order, within one job — never decoupled, or an
    async purge could land after the re-ingest and delete the freshly
    rebuilt table (same deterministic table_id)."""
    from app.worker import kinds as kinds_mod

    order = []
    monkeypatch.setattr(
        "app.api.collections._purge_derived_tabular_row_for_file",
        lambda corpus_id, file_id: order.append(("purge", corpus_id, file_id)),
    )
    monkeypatch.setattr(
        "src.ingest.runner.ingest_file",
        lambda file_id: order.append(("ingest", file_id)) or "indexed",
    )

    kinds_mod._run_collections_purge({"corpus_id": "c1", "file_id": "f1", "reingest_after_purge": True})

    assert order == [("purge", "c1", "f1"), ("ingest", "f1")]


def test_collections_purge_handler_no_reingest_when_flag_absent(monkeypatch):
    from app.worker import kinds as kinds_mod

    ingest_called = []
    monkeypatch.setattr("app.api.collections._purge_derived_tabular_row_for_file", lambda c, f: None)
    monkeypatch.setattr("src.ingest.runner.ingest_file", lambda file_id: ingest_called.append(file_id))

    kinds_mod._run_collections_purge({"corpus_id": "c1", "file_id": "f1"})

    assert ingest_called == [], "ingest_file must not run unless reingest_after_purge is set"


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


def test_reingest_enqueues_ordered_job_when_api_only(seeded_app, tmp_path, monkeypatch):
    """On a role-split deployment, reingest must NOT decouple an async purge
    from an in-process ingest — that ordering is exactly what let a purge
    delete a freshly re-ingested table (Devin finding on #981). It must
    enqueue one ordered collections-purge(reingest_after_purge=True) job and
    do nothing in-process."""
    from app.roles import Role
    from src.repositories import corpus_files_repo, file_corpora_repo

    monkeypatch.setattr("app.roles.role_enabled", lambda r: r != Role.WORKER)
    spy = _SpyJobs()
    monkeypatch.setattr("src.repositories.jobs_repo", lambda: spy)
    purge_calls = []
    monkeypatch.setattr(
        "app.api.collections._purge_derived_tabular_row_for_file",
        lambda c, f: purge_calls.append((c, f)),
    )

    col_id = file_corpora_repo().create(name="ri2", slug="ri2", description=None, created_by="u1")
    csv = tmp_path / "d.csv"
    csv.write_text("a,b\n1,2\n", encoding="utf-8")
    fid = corpus_files_repo().add(
        corpus_id=col_id,
        filename="d.csv",
        sha256="s",
        file_type="csv",
        size_bytes=csv.stat().st_size,
        storage_path=str(csv),
    )

    c = seeded_app["client"]
    r = c.post(
        f"/api/collections/{col_id}/files/{fid}/reingest",
        headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
    )

    assert r.status_code == 202, r.text
    assert purge_calls == [], "api-only replica must not purge in-process"
    assert [e["kind"] for e in spy.enqueued] == ["collections-purge"]
    assert spy.enqueued[0]["payload"] == {"corpus_id": col_id, "file_id": fid, "reingest_after_purge": True}
    # Status is still 'pending' — the real ingest hasn't run in-process (it
    # only runs when the enqueued job reaches the worker plane).
    assert corpus_files_repo().get(fid)["processing_status"] == "pending"
