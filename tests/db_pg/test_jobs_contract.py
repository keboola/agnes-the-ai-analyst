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
from typing import Any

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


def _repo_from_resource(backend: str, resource: Any):
    if backend == "duckdb":
        from src.repositories.jobs import JobsRepository

        return JobsRepository(resource)
    from src.repositories.jobs_pg import JobsPgRepository

    return JobsPgRepository(resource)


@pytest.fixture(params=["duckdb", "pg"])
def repo_factory(request, tmp_path, pg_engine, monkeypatch):
    """Yields a zero-arg callable that builds a FRESH repo instance on
    every call, all sharing the same underlying connection (DuckDB) or
    engine (PG).

    This mirrors real usage: the production factory (``jobs_repo()``)
    constructs a brand-new repo object per call/request, wrapping the
    same underlying connection/engine — it never hands out a shared repo
    instance to concurrent callers. A test that reuses ONE repo instance
    across threads (like the plain ``repo`` fixture) would keep any
    per-instance state (e.g. a lock) alive and shared too, silently
    masking bugs where that state should have been module-level instead.
    Use this fixture for concurrency regression tests.
    """
    backend = request.param
    if backend == "duckdb":
        repo, conn = _make_duckdb_repo(tmp_path)
        yield lambda: _repo_from_resource("duckdb", conn)
        conn.close()
    else:
        repo, _ = _make_pg_repo(pg_engine, monkeypatch)
        import src.db_pg as db_pg

        engine = db_pg.get_engine()
        yield lambda: _repo_from_resource("pg", engine)


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


def test_concurrent_enqueue_same_key_dedups_to_exactly_one_row(repo_factory):
    """Regression test for the PG dedup race: 8 threads enqueue the same
    ``idempotency_key`` concurrently. Under a plain SELECT-then-INSERT on
    Postgres (READ COMMITTED), concurrent transactions can each miss the
    others' uncommitted row and all insert — empirically confirmed to
    produce 8 rows. Exactly one row must exist afterward, on both
    backends (the DuckDB path exercises the module-level enqueue lock
    instead of a cross-transaction race).

    Each thread builds its OWN repo instance via ``repo_factory`` (as the
    production ``jobs_repo()`` factory does per caller) rather than
    sharing one repo object — this is what caught the DuckDB bug where
    the dedup lock lived on ``self`` instead of at module scope: with a
    shared instance the shared lock papered over the race; with separate
    instances (all wrapping the same underlying connection) it did not.
    """
    n = 8
    barrier = threading.Barrier(n)
    errors: list[BaseException] = []

    def worker(i: int) -> None:
        try:
            barrier.wait(timeout=5)
            repo_factory().enqueue("send_email", {"i": i}, idempotency_key="race-key")
        except BaseException as exc:  # noqa: BLE001 - surfaced via errors list
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"enqueue raised under concurrency: {errors}"
    matching = repo_factory().list(kind="send_email", limit=50)
    assert len(matching) == 1


# ---------------------------------------------------------------------------
# PG-only: fallback-miss retry
# ---------------------------------------------------------------------------


def test_pg_enqueue_retries_insert_when_fallback_select_misses(pg_engine, monkeypatch):
    """Regression test for the PG fallback-miss race.

    Sequence: our INSERT loses the ``ON CONFLICT`` race (another job
    already holds the key as 'queued'/'running') -> before our fallback
    SELECT runs, the winning row's status flips to 'done' (job finished)
    -> the fallback SELECT (``WHERE status IN ('queued', 'running')``)
    finds nothing. The key is legitimately free for reuse at that point
    (the partial unique index no longer excludes it), so ``enqueue()``
    must retry the INSERT and succeed, rather than asserting/crashing on
    ``row is None``.

    Simulated via a SQLAlchemy ``after_cursor_execute`` event: right after
    our own ``INSERT ... ON CONFLICT ... DO NOTHING`` executes and misses
    (finds the key still held by 'winner'), we flip the winner's row to
    'done' out-of-band — a separate connection, committed immediately —
    before ``enqueue()``'s code moves on to run the fallback SELECT. Under
    READ COMMITTED that SELECT then deterministically observes the
    post-flip state and misses too, forcing the retry path.
    """
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg

    db_pg.dispose()
    engine = db_pg.get_engine()

    from src.repositories.jobs_pg import JobsPgRepository

    repo = JobsPgRepository(engine)

    # Seed the "winner": a queued job holding the idempotency key.
    winner = repo.enqueue("send_email", {"who": "winner"}, idempotency_key="flip-key")

    import sqlalchemy as sa

    flipped = {"done": False}

    def _flip_winner_to_done(conn, cursor, statement, parameters, context, executemany):
        # Fires right after OUR INSERT ... ON CONFLICT ... DO NOTHING
        # executes and misses (the key is still held by 'winner'). Flip
        # 'winner' to 'done' on a separate connection, committed
        # immediately, before enqueue()'s code moves on to the fallback
        # SELECT — so that SELECT deterministically misses too. Guarded
        # to fire only once so the retried INSERT doesn't re-trigger it.
        if not flipped["done"] and "INSERT INTO jobs" in statement and "ON CONFLICT" in statement:
            flipped["done"] = True
            with engine.connect() as side_conn:
                side_conn.execute(
                    sa.text("UPDATE jobs SET status = 'done' WHERE id = :id"),
                    {"id": winner["id"]},
                )
                side_conn.commit()

    sa.event.listen(engine, "after_cursor_execute", _flip_winner_to_done)
    try:
        # This enqueue call loses the initial INSERT race (key still held
        # by 'winner' at INSERT time), then the event hook flips 'winner'
        # to 'done' right after — before the fallback SELECT runs — so
        # that SELECT must miss too, forcing a retry INSERT that should
        # succeed.
        second = repo.enqueue("send_email", {"who": "second"}, idempotency_key="flip-key")
    finally:
        sa.event.remove(engine, "after_cursor_execute", _flip_winner_to_done)

    assert flipped["done"], "test setup bug: the fallback SELECT hook never fired"
    assert second["id"] != winner["id"]
    assert second["status"] == "queued"
    assert second["payload_json"] == {"who": "second"}

    # Both rows now exist: the original (now done) and the new retried insert.
    all_matching = repo.list(kind="send_email", limit=50)
    ids = {r["id"] for r in all_matching}
    assert {winner["id"], second["id"]} <= ids
