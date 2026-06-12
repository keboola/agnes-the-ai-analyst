"""Startup Alembic revision floor (issue #636).

The Postgres backend has no self-migration: after an in-app
DuckDB→PG switch, pulling a newer app image leaves the PG schema at
whatever revision the migrate flow stamped. The app boots "healthy" but
500s every write touching a post-stamp column. ``assert_pg_at_head()``
is the fail-closed floor — it refuses to boot when the DB is behind head.

These tests stamp the DB one revision below head (derived, never
hardcoded), then assert:

  (i)   behind head            → RuntimeError naming the lagging revision
  (ii)  at head                → no raise
  (iii) escape-hatch env set   → no raise even when behind
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _alembic_config(db_url: str):
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = db_url
    return cfg


def _head_and_prev() -> tuple[str, str]:
    """Return (head_revision, revision_immediately_below_head).

    Derived from the live script directory so the test never hardcodes a
    revision id that a future migration would invalidate.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    script = ScriptDirectory.from_config(cfg)

    head = script.get_current_head()
    prev = script.get_revision(head).down_revision
    assert prev, "expected at least two revisions in the chain"
    # down_revision can be a tuple for merge revisions; the chain here is
    # linear, but be defensive.
    if isinstance(prev, (tuple, list)):
        prev = prev[0]
    return head, prev


@pytest.fixture
def pg_under_app(pg_engine, monkeypatch):
    """Point ``src.db_pg.get_engine()`` at the per-test pgserver DB.

    ``assert_pg_at_head()`` resolves its connection through the module
    singleton, which reads ``DATABASE_URL``. Set it to the test engine's
    URL and dispose the singleton so the next ``get_engine()`` rebuilds
    against it. Tear down by disposing again so no other test inherits
    this engine.
    """
    import src.db_pg as db_pg

    monkeypatch.setenv("DATABASE_URL", str(pg_engine.url))
    monkeypatch.delenv("AGNES_DB_URL", raising=False)
    db_pg.dispose()
    yield pg_engine
    db_pg.dispose()


def test_raises_when_behind_head(pg_under_app):
    """DB stamped one revision below head → RuntimeError naming it."""
    from alembic import command

    from src.db_pg import assert_pg_at_head

    head, prev = _head_and_prev()
    cfg = _alembic_config(str(pg_under_app.url))
    command.stamp(cfg, prev)

    with pytest.raises(RuntimeError) as exc:
        assert_pg_at_head()

    msg = str(exc.value)
    assert prev in msg, f"error should name the lagging revision {prev!r}: {msg}"
    assert head in msg, f"error should name the head revision {head!r}: {msg}"


def test_raises_when_never_stamped(pg_under_app):
    """No alembic_version row at all (current is None) → still raises."""
    from src.db_pg import assert_pg_at_head

    # pg_engine hands out a freshly DROP/CREATE'd public schema, so the
    # DB has never been stamped.
    head, _prev = _head_and_prev()
    with pytest.raises(RuntimeError) as exc:
        assert_pg_at_head()
    assert head in str(exc.value)


def test_no_raise_at_head(pg_under_app):
    """DB upgraded to head → assert_pg_at_head is a no-op."""
    from alembic import command

    from src.db_pg import assert_pg_at_head

    cfg = _alembic_config(str(pg_under_app.url))
    command.upgrade(cfg, "head")

    assert_pg_at_head()  # must not raise


def test_escape_hatch_skips_check(pg_under_app, monkeypatch):
    """AGNES_SKIP_PG_REVISION_CHECK=1 → no raise even when behind."""
    from alembic import command

    from src.db_pg import assert_pg_at_head

    head, prev = _head_and_prev()
    cfg = _alembic_config(str(pg_under_app.url))
    command.stamp(cfg, prev)

    monkeypatch.setenv("AGNES_SKIP_PG_REVISION_CHECK", "1")
    assert_pg_at_head()  # must not raise despite being behind


def test_raises_db_ahead_when_revision_unknown(pg_under_app):
    """#641 review: a DB stamped with a revision this image's scripts don't
    contain (app rolled back after a newer image migrated) must say AHEAD —
    the remedy (roll the image forward / restore backup) is the opposite of
    the behind-head one (upgrade head)."""
    import sqlalchemy as sa

    from src.db_pg import assert_pg_at_head, get_engine

    head, _prev = _head_and_prev()
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(sa.text(
            "CREATE TABLE IF NOT EXISTS alembic_version "
            "(version_num VARCHAR(32) NOT NULL)"
        ))
        conn.execute(sa.text("DELETE FROM alembic_version"))
        conn.execute(sa.text(
            "INSERT INTO alembic_version (version_num) VALUES ('ffffffffffff')"
        ))

    with pytest.raises(RuntimeError) as exc:
        assert_pg_at_head()

    msg = str(exc.value)
    assert "AHEAD" in msg, f"unknown revision must report DB-ahead: {msg}"
    assert "ffffffffffff" in msg
    assert head in msg


# ---------------------------------------------------------------------------
# ensure_pg_at_head — DuckDB-parity self-migration (issue #636, part 2).
#
# assert_pg_at_head turned silent write-breakage into a boot refusal; on
# deployments with no migrate step that refusal is a crash-loop on every
# release that carries a migration. ensure_pg_at_head closes the loop: when
# the DB is BEHIND it applies the pending migrations itself (serialized via
# a Postgres advisory lock), mirroring the DuckDB ladder's self-migration.
# AHEAD stays fail-closed (auto-rollback is never safe), and
# AGNES_PG_AUTO_MIGRATE=0 opts out for pipeline-controlled deployments.
# ---------------------------------------------------------------------------


def _current_revision(engine) -> str | None:
    import sqlalchemy as sa

    with engine.connect() as conn:
        try:
            return conn.execute(
                sa.text("SELECT version_num FROM alembic_version")
            ).scalar()
        except Exception:
            return None


def test_ensure_auto_migrates_when_behind(pg_under_app):
    """DB at the revision below head → ensure runs the pending migration."""
    from alembic import command

    from src.db_pg import ensure_pg_at_head

    head, prev = _head_and_prev()
    cfg = _alembic_config(str(pg_under_app.url))
    command.upgrade(cfg, prev)  # real schema at prev, not just a stamp

    ensure_pg_at_head()  # must not raise

    assert _current_revision(pg_under_app) == head


def test_ensure_noop_at_head(pg_under_app):
    from alembic import command

    from src.db_pg import ensure_pg_at_head

    cfg = _alembic_config(str(pg_under_app.url))
    command.upgrade(cfg, "head")

    ensure_pg_at_head()  # must not raise

    head, _ = _head_and_prev()
    assert _current_revision(pg_under_app) == head


def test_ensure_opt_out_keeps_fail_closed(pg_under_app, monkeypatch):
    """AGNES_PG_AUTO_MIGRATE=0 → behind-head still refuses to boot."""
    from alembic import command

    from src.db_pg import ensure_pg_at_head

    head, prev = _head_and_prev()
    cfg = _alembic_config(str(pg_under_app.url))
    command.upgrade(cfg, prev)
    monkeypatch.setenv("AGNES_PG_AUTO_MIGRATE", "0")

    with pytest.raises(RuntimeError) as exc:
        ensure_pg_at_head()
    assert prev in str(exc.value)
    assert _current_revision(pg_under_app) == prev  # untouched


def test_ensure_ahead_still_fails_closed(pg_under_app):
    """A revision unknown to this image (app rollback) is never auto-fixed."""
    import sqlalchemy as sa
    from alembic import command

    from src.db_pg import ensure_pg_at_head

    cfg = _alembic_config(str(pg_under_app.url))
    command.upgrade(cfg, "head")
    with pg_under_app.connect() as conn:
        conn.execute(
            sa.text("UPDATE alembic_version SET version_num = 'ffffffffffff'")
        )
        conn.commit()

    with pytest.raises(RuntimeError) as exc:
        ensure_pg_at_head()
    assert "AHEAD" in str(exc.value)


def test_ensure_skip_env_short_circuits(pg_under_app, monkeypatch):
    """AGNES_SKIP_PG_REVISION_CHECK=1 → no migration, no raise."""
    from alembic import command

    from src.db_pg import ensure_pg_at_head

    _head, prev = _head_and_prev()
    cfg = _alembic_config(str(pg_under_app.url))
    command.upgrade(cfg, prev)
    monkeypatch.setenv("AGNES_SKIP_PG_REVISION_CHECK", "1")

    ensure_pg_at_head()  # must not raise

    assert _current_revision(pg_under_app) == prev  # untouched


def test_ensure_concurrent_callers_serialize(pg_under_app):
    """Two concurrent callers (app + a second replica) both succeed; the
    advisory lock serializes the upgrade and the late acquirer no-ops."""
    import threading

    from alembic import command

    from src.db_pg import ensure_pg_at_head

    head, prev = _head_and_prev()
    cfg = _alembic_config(str(pg_under_app.url))
    command.upgrade(cfg, prev)

    errors = []

    def run():
        try:
            ensure_pg_at_head()
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    assert not errors
    assert _current_revision(pg_under_app) == head
