"""Cross-engine contract tests for the metric_definitions repository.

Scoped to the one property both backends were silently disagreeing with the
truth on: a metric created WITHOUT a grain used to be stamped ``"monthly"`` by
a Python default on both repos (and by a column default underneath them).
``src/semantic/projection.py`` is the only caller in ``src/``, ``app/``,
``cli/`` and ``connectors/`` that omits the argument, so every Ossie-projected
metric claimed a monthly grain nobody declared — and ``agnes catalog --metrics
--show`` prints ``Grain:`` unconditionally, which put the invented value in
front of the agent as fact.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _make_duckdb_repo(tmp_path):
    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb
    from src.repositories.metrics import MetricRepository

    conn = _open_duckdb(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return MetricRepository(conn), conn


def _make_pg_repo(pg_engine, monkeypatch):
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

    from src.repositories.metrics_pg import MetricPgRepository

    return MetricPgRepository(db_pg.get_engine()), None


@pytest.fixture(params=["duckdb", "pg"])
def repo(request, tmp_path, pg_engine, monkeypatch):
    if request.param == "duckdb":
        r, conn = _make_duckdb_repo(tmp_path)
        yield r
        conn.close()
    else:
        r, _ = _make_pg_repo(pg_engine, monkeypatch)
        yield r


def test_omitting_grain_stores_no_grain(repo):
    """No caller declared a grain, so none is reported. The alternative is not
    a harmless default: it is a claim about the metric's time dimension that
    the reader has no way to tell apart from a declared one."""
    repo.create(
        id="ossie/model/revenue",
        name="revenue",
        display_name="revenue",
        category="ossie",
        sql="SUM(amount)",
    )

    assert repo.get("ossie/model/revenue")["grain"] is None


def test_grain_is_nullable_on_both_ladders(tmp_path, pg_engine):
    """The divergence the round-trip test above only shows indirectly: DuckDB
    declared ``grain`` nullable from the start while Postgres got it as
    ``nullable=False`` in migration 0005, and every caller passing a grain kept
    that hidden. Asserted on the column itself so a future ladder step that
    re-tightens one side fails here rather than in whichever backend an
    instance happens to run."""
    from alembic import command
    from alembic.config import Config

    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb

    conn = _open_duckdb(str(tmp_path / "duck.duckdb"))
    try:
        _ensure_schema(conn)
        duck_nullable = conn.execute(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = 'metric_definitions' AND column_name = 'grain'"
        ).fetchone()[0]
    finally:
        conn.close()

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")
    with pg_engine.connect() as pg_conn:
        pg_nullable = pg_conn.exec_driver_sql(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = 'metric_definitions' AND column_name = 'grain'"
        ).scalar()

    assert str(duck_nullable).upper() == "YES"
    assert str(pg_nullable).upper() == "YES"


def test_an_explicit_grain_still_round_trips(repo):
    repo.create(
        id="ossie/model/mrr",
        name="mrr",
        display_name="mrr",
        category="ossie",
        sql="SUM(mrr_amount)",
        grain="monthly",
    )

    assert repo.get("ossie/model/mrr")["grain"] == "monthly"
