"""B1-NEW — concurrent POST /migrate calls cannot both win.

Test approach: Option A (threading with Barrier).

The post-fix structure is:
  [pre-lock: cheap rejections — no state reads]
  MigrationLock().__enter__()          ← T1 acquires; T2 blocks (LOCK_NB)
  read_backend_state()                 ← re-read under lock
  _current_job_id()                    ← re-check under lock → T2 sees T1's job
  validate_transition(...)             ← under lock
  write_backend_state(in_progress)
  write job file

The test synchronizes both threads with a Barrier placed at the top of
``fire()`` (BEFORE ``start_migration`` is called) to ensure both threads
are alive and scheduled before either starts.  This is sufficient because
the fix serializes the entire validation+write block under the flock; the
second thread WILL see the first thread's job after blocking on the flock
and re-reading state.

Why threading works here:
``fcntl.flock`` with LOCK_NB IS thread-aware on both macOS and Linux when
threads use separate file descriptors (``os.open`` returns a new fd per
call).  Each call to ``MigrationLock().__enter__`` opens a fresh fd —
so the second thread's LOCK_NB acquire blocks until the first releases.
Confirmed empirically on this platform (macOS Darwin 25.x, Python 3.12).
"""
from __future__ import annotations

import json
import threading
from pathlib import Path


def test_concurrent_migrate_only_one_wins(tmp_path: Path, monkeypatch) -> None:
    """Two threads invoke ``start_migration`` simultaneously.

    Pre-B1-NEW: both threads passed ``validate_transition`` before either
    took the flock, so both wrote pending jobs — two winners, zero 409s.

    Post-B1-NEW: all state reads + validation happen INSIDE the flock.
    The second thread blocks at flock acquisition, then re-reads state and
    finds the first thread's pending job → raises HTTPException(409).
    """
    import os
    from app.api import db_state

    # ── Setup: point all path helpers at tmp_path ──────────────────────────
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    jobs_dir = state_dir / "db-jobs"
    jobs_dir.mkdir()
    instance_yaml = state_dir / "instance.yaml"
    # Start from DUCKDB (the default stable state).
    instance_yaml.write_text("database:\n  backend: duckdb\n")
    lock_path = state_dir / "db-migration.lock"

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(db_state, "_jobs_dir", lambda: jobs_dir)

    import src.db_state_machine as _sm
    monkeypatch.setattr(_sm, "_OVERLAY_PATH", instance_yaml)
    monkeypatch.setattr(_sm, "_LOCK_PATH", lock_path)

    # Set POSTGRES_PASSWORD so side_car URL construction doesn't 500.
    monkeypatch.setenv("POSTGRES_PASSWORD", "testpw")
    # Allow reserved IPs in cloud_url (MED-2 bypass for test fixtures).
    monkeypatch.setenv("AGNES_ALLOW_RESERVED_CLOUD_URL", "1")

    # ── Barrier: both threads are alive and ready before either starts ────
    # The barrier does NOT go inside start_migration — that would deadlock
    # under the post-fix structure because T1 holds the flock while T2
    # waits to acquire it, so T2 never reaches the barrier.  Instead we
    # synchronize at the entry of fire() so both threads are scheduled and
    # running before either calls start_migration.
    barrier = threading.Barrier(2)

    results: list[dict] = []
    exc_results: list[BaseException] = []

    def fire(target: str, cloud_url: str | None = None) -> None:
        try:
            barrier.wait(timeout=5)   # ensure both threads start "at the same time"
            out = db_state.start_migration(
                payload=db_state.MigrateRequest(
                    target=target,
                    cloud_url=cloud_url,
                )
            )
            results.append(out)
        except BaseException as e:
            exc_results.append(e)

    t1 = threading.Thread(target=fire, args=("side_car",), name="T1-side_car")
    t2 = threading.Thread(
        target=fire,
        args=("cloud", "postgresql+psycopg://u:p@db.example.com:5432/agnes"),
        name="T2-cloud",
    )
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    # ── Assertions ────────────────────────────────────────────────────────
    # Exactly one of the two must succeed (status=pending, job_id present).
    assert len(results) == 1, (
        f"Expected exactly one winner among two concurrent /migrate calls.\n"
        f"  winners:  {results}\n"
        f"  failures: {[repr(e) for e in exc_results]}"
    )
    winner = results[0]
    assert winner.get("status") == "pending"
    assert winner.get("job_id")

    # The loser must have raised HTTPException 409.
    from fastapi import HTTPException
    assert len(exc_results) == 1, (
        f"Expected exactly one loser, got {[repr(e) for e in exc_results]}"
    )
    loser_exc = exc_results[0]
    assert isinstance(loser_exc, HTTPException) and loser_exc.status_code == 409, (
        f"Loser must return HTTP 409 conflict, got {loser_exc!r}"
    )
    # The 409 detail must say "already in progress" — the job_id may not
    # appear in the detail if the winner is still mid-write when the loser
    # is rejected at lock acquisition.  The key contract is 409 + wording.
    assert "already in progress" in str(loser_exc.detail).lower(), (
        f"409 detail should say 'already in progress'; got: {loser_exc.detail!r}"
    )

    # Only one pending job file must exist on disk.
    job_files = list(jobs_dir.glob("*.json"))
    assert len(job_files) == 1, (
        f"Expected exactly one job file, found: {[p.name for p in job_files]}"
    )
    written_job = json.loads(job_files[0].read_text())
    assert written_job["job_id"] == winner["job_id"], (
        "Job file job_id must match the winner's job_id"
    )
    assert written_job["status"] == "pending"
