"""Scheduler last_run survives restart (three-plane §3.3 catch-up durability).

Without persistence, a scheduler restart reset last_run to None for every
job, and is_table_due(None) is always True — so every job re-fired on the
first post-grace tick (a burst of duplicate fires; harmless for idempotency-
keyed enqueue jobs, audit noise + wasted requests for the HTTP jobs).
"""

from __future__ import annotations

import json

from services.scheduler import __main__ as sched


def _point_at(tmp_path, monkeypatch):
    path = tmp_path / "state" / "scheduler_last_run.json"
    monkeypatch.setattr(sched, "_LAST_RUN_PATH", path)
    return path


def test_persist_then_load_roundtrip(tmp_path, monkeypatch):
    _point_at(tmp_path, monkeypatch)
    sched._persist_last_run({"a": "2026-01-01T00:00:00", "b": None})
    loaded = sched._load_last_run({"a", "b"})
    assert loaded == {"a": "2026-01-01T00:00:00", "b": None}


def test_load_missing_file_seeds_all_none(tmp_path, monkeypatch):
    _point_at(tmp_path, monkeypatch)
    assert sched._load_last_run({"a", "b"}) == {"a": None, "b": None}


def test_load_drops_stale_keys_and_seeds_new(tmp_path, monkeypatch):
    path = _point_at(tmp_path, monkeypatch)
    path.parent.mkdir(parents=True)
    # Persisted state includes a job that no longer exists ("gone") and
    # lacks one that now does ("added").
    path.write_text(json.dumps({"kept": "2026-01-01T00:00:00", "gone": "2026-01-02T00:00:00"}))
    loaded = sched._load_last_run({"kept", "added"})
    assert loaded == {"kept": "2026-01-01T00:00:00", "added": None}


def test_load_tolerates_corrupt_file(tmp_path, monkeypatch):
    path = _point_at(tmp_path, monkeypatch)
    path.parent.mkdir(parents=True)
    path.write_text("{ this is not json")
    # Falls back to empty (all-None) rather than crashing the scheduler.
    assert sched._load_last_run({"a"}) == {"a": None}


def test_persist_is_atomic_and_best_effort(tmp_path, monkeypatch):
    # Point at a path whose parent cannot be created (a file where a dir is
    # expected) → persist must swallow the error, not raise.
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    monkeypatch.setattr(sched, "_LAST_RUN_PATH", blocker / "state" / "last_run.json")
    sched._persist_last_run({"a": "2026-01-01T00:00:00"})  # must not raise


def test_run_job_persists_last_run(tmp_path, monkeypatch):
    import threading

    path = _point_at(tmp_path, monkeypatch)
    monkeypatch.setattr(sched, "_call_api", lambda *a, **kw: True)

    last_run: dict[str, str | None] = {"verification": None}
    sched._run_job(
        "verification",
        "/api/admin/run-x",
        "POST",
        60,
        "2026-05-05T12:00:00",
        last_run,
        {"verification"},
        threading.Lock(),
    )
    assert json.loads(path.read_text())["verification"] == "2026-05-05T12:00:00"
