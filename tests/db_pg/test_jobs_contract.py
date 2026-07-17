"""Cross-engine contract tests for the ``jobs`` repository.

Parametrises over [DuckDB impl, Postgres impl]. The same calls go to
both; the same return shapes must come back. Foundation for the wave-2B
worker runtime — this task covers enqueue/get/list + idempotency dedup
only (claim/lease lifecycle + worker loop are later tasks).

Follows the pattern established in ``test_ticket_contract.py``.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# repo construction helpers — one per backend
# ---------------------------------------------------------------------------


def _make_duckdb_repo(tmp_path):
    # Route through `_open_duckdb` (rather than bare `duckdb.connect`) so
    # the session timezone is pinned to UTC — keeps `tests/db_pg/`'s
    # `test_no_bare_duckdb_connect_in_production_code` regression guard
    # green on new files.
    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb
    from src.repositories.jobs import JobsRepository

    conn = _open_duckdb(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return JobsRepository(conn), conn


def _make_pg_repo(pg_engine, monkeypatch):
    """Run migrations on the per-test PG engine, then return a PG repo."""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg

    db_pg.dispose()
    db_pg.get_engine()

    from src.repositories.jobs_pg import JobsPgRepository

    return JobsPgRepository(db_pg.get_engine()), None


@pytest.fixture(params=["duckdb", "pg"])
def repo(request, tmp_path, pg_engine, monkeypatch):
    """Yields a ``jobs`` repo bound to either DuckDB or PG."""
    backend = request.param
    if backend == "duckdb":
        repo, conn = _make_duckdb_repo(tmp_path)
        yield repo
        if conn is not None:
            conn.close()
    else:
        repo, _ = _make_pg_repo(pg_engine, monkeypatch)
        yield repo


# ---------------------------------------------------------------------------
# contract tests — same calls, same answers from both engines
# ---------------------------------------------------------------------------


def test_enqueue_returns_queued_row(repo):
    row = repo.enqueue("send_email", {"to": "a@example.com"})
    assert row["id"]
    assert row["status"] == "queued"
    assert row["kind"] == "send_email"
    assert row["payload_json"] == {"to": "a@example.com"}
    assert row["priority"] == 0
    assert row["attempts"] == 0
    assert row["max_attempts"] == 3
    assert row["created_at"] is not None


def test_enqueue_defaults_payload_to_empty_dict(repo):
    row = repo.enqueue("noop", {})
    assert row["payload_json"] == {}


def test_get_roundtrip(repo):
    enqueued = repo.enqueue("send_email", {"to": "b@example.com"}, priority=5, max_attempts=7)
    fetched = repo.get(enqueued["id"])
    assert fetched is not None
    assert fetched["id"] == enqueued["id"]
    assert fetched["kind"] == "send_email"
    assert fetched["priority"] == 5
    assert fetched["max_attempts"] == 7
    assert fetched["payload_json"] == {"to": "b@example.com"}


def test_get_unknown_returns_none(repo):
    assert repo.get("does-not-exist") is None


def test_enqueue_respects_run_after(repo):
    run_after = datetime.now(timezone.utc) + timedelta(hours=1)
    row = repo.enqueue("scheduled_task", {}, run_after=run_after)
    fetched = repo.get(row["id"])
    assert fetched["run_after"] is not None


def test_idempotency_dedup_returns_same_job_while_queued(repo):
    first = repo.enqueue("send_email", {"to": "c@example.com"}, idempotency_key="dup-key-1")
    second = repo.enqueue("send_email", {"to": "different@example.com"}, idempotency_key="dup-key-1")
    assert second["id"] == first["id"]
    # the dedup hit returned the ORIGINAL row, not a re-insert with the
    # second call's payload
    assert second["payload_json"] == {"to": "c@example.com"}
    # only one row was actually created
    assert len(repo.list(kind="send_email")) == 1


def test_no_dedup_without_idempotency_key(repo):
    repo.enqueue("send_email", {"to": "d@example.com"})
    repo.enqueue("send_email", {"to": "d@example.com"})
    assert len(repo.list(kind="send_email")) == 2


def test_distinct_idempotency_keys_do_not_collide(repo):
    a = repo.enqueue("send_email", {}, idempotency_key="key-a")
    b = repo.enqueue("send_email", {}, idempotency_key="key-b")
    assert a["id"] != b["id"]


def test_list_filters_by_status(repo):
    repo.enqueue("a", {})
    repo.enqueue("b", {})
    all_jobs = repo.list()
    assert len(all_jobs) == 2
    queued = repo.list(status="queued")
    assert len(queued) == 2
    done = repo.list(status="done")
    assert done == []


def test_list_filters_by_kind(repo):
    repo.enqueue("alpha", {})
    repo.enqueue("beta", {})
    repo.enqueue("alpha", {})
    assert len(repo.list(kind="alpha")) == 2
    assert len(repo.list(kind="beta")) == 1
    assert repo.list(kind="gamma") == []


def test_list_respects_limit(repo):
    for i in range(5):
        repo.enqueue("bulk", {"i": i})
    assert len(repo.list(kind="bulk", limit=2)) == 2
    assert len(repo.list(kind="bulk", limit=50)) == 5


def test_concurrent_enqueue_same_key_dedups_to_exactly_one_row(repo):
    """Regression test for the PG dedup race: 8 threads enqueue the same
    ``idempotency_key`` concurrently. Under a plain SELECT-then-INSERT on
    Postgres (READ COMMITTED), concurrent transactions can each miss the
    others' uncommitted row and all insert — empirically confirmed to
    produce 8 rows. Exactly one row must exist afterward, on both
    backends (the DuckDB path exercises the repository's in-process lock
    instead of a cross-transaction race).
    """
    n = 8
    barrier = threading.Barrier(n)
    errors: list[BaseException] = []

    def worker(i: int) -> None:
        try:
            barrier.wait(timeout=5)
            repo.enqueue("send_email", {"i": i}, idempotency_key="race-key")
        except BaseException as exc:  # noqa: BLE001 - surfaced via errors list
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"enqueue raised under concurrency: {errors}"
    matching = repo.list(kind="send_email", limit=50)
    assert len(matching) == 1
