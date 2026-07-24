"""A fresh PG enqueue pings the ``agnes_jobs`` NOTIFY channel so an idle
worker lane slot (LISTENing via app.worker.wakeup) claims immediately
instead of waiting out its poll interval (three-plane §3.3). A deduped
enqueue does NOT notify — the already-queued job will be claimed anyway."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def pg_jobs_repo(pg_engine, monkeypatch):
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg

    db_pg.dispose()
    from src.repositories.jobs_pg import JobsPgRepository

    return JobsPgRepository(db_pg.get_engine()), db_pg.get_engine()


def _listen_conn(engine):
    """A raw psycopg connection LISTENing on agnes_jobs, autocommit so the
    LISTEN registers immediately."""
    import psycopg

    url = engine.url.set(drivername="postgresql").render_as_string(hide_password=False)
    conn = psycopg.connect(url, autocommit=True)
    conn.execute("LISTEN agnes_jobs")
    return conn


def test_fresh_enqueue_emits_notify(pg_jobs_repo):
    repo, engine = pg_jobs_repo
    conn = _listen_conn(engine)
    try:
        repo.enqueue("data-refresh", {}, idempotency_key="k-notify-1")
        notifies = list(conn.notifies(timeout=5.0, stop_after=1))
        assert notifies, "fresh enqueue must emit a NOTIFY on agnes_jobs"
        assert notifies[0].channel == "agnes_jobs"
    finally:
        conn.close()


def test_deduped_enqueue_does_not_notify(pg_jobs_repo):
    repo, engine = pg_jobs_repo
    # First enqueue creates the row (and notifies); drain that notification.
    conn = _listen_conn(engine)
    try:
        repo.enqueue("data-refresh", {}, idempotency_key="k-notify-2")
        assert list(conn.notifies(timeout=5.0, stop_after=1)), "first enqueue should notify"

        # Second enqueue with the same key dedups (row already queued) → no
        # new NOTIFY. Give it a short window; expect nothing.
        r2 = repo.enqueue("data-refresh", {}, idempotency_key="k-notify-2")
        assert r2["deduped"] is True
        extra = list(conn.notifies(timeout=1.0))
        assert extra == [], "a deduped enqueue must not emit a second NOTIFY"
    finally:
        conn.close()
