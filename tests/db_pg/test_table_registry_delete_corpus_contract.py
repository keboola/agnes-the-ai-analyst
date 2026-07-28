"""Cross-engine contract for TableRegistry.delete_for_corpus.

Verifies that deleting all table_registry rows for a collection corpus
works identically on DuckDB and Postgres.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# Shared fixture wiring (mirrors test_ported_methods_contract.py pattern)
# ---------------------------------------------------------------------------


class _Ctx:
    def __init__(self, backend, conn=None, engine=None):
        self.backend = backend
        self._conn = conn
        self._engine = engine

    def table_registry(self):
        if self.backend == "duckdb":
            from src.repositories.table_registry import TableRegistryRepository

            return TableRegistryRepository(self._conn)
        from src.repositories.table_registry_pg import TableRegistryPgRepository

        return TableRegistryPgRepository(self._engine)


@pytest.fixture(params=["duckdb", "pg"])
def ctx(request, tmp_path, pg_engine, monkeypatch):
    backend = request.param
    if backend == "duckdb":
        import duckdb as _duckdb
        from src.db import _ensure_schema

        conn = _duckdb.connect(str(tmp_path / "duck.duckdb"))
        _ensure_schema(conn)
        yield _Ctx("duckdb", conn=conn)
        conn.close()
    else:
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

        yield _Ctx("pg", engine=db_pg.get_engine())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_delete_for_corpus_removes_matching_rows(ctx):
    """delete_for_corpus() removes all rows with source_type='collection'
    and bucket matching the corpus_id, leaving other rows intact."""
    reg = ctx.table_registry()

    corpus_id = "col_abc123"
    reg.register(id="t1", name="t1", source_type="collection", bucket=corpus_id, query_mode="local")
    reg.register(id="t2", name="t2", source_type="collection", bucket=corpus_id, query_mode="local")
    reg.register(id="t3", name="t3", source_type="keboola", bucket="other", query_mode="local")

    reg.delete_for_corpus(corpus_id)

    assert reg.get("t1") is None
    assert reg.get("t2") is None
    # unrelated row must survive
    assert reg.get("t3") is not None


def test_delete_for_corpus_returns_deleted_ids(ctx):
    """delete_for_corpus() returns the list of ids that were removed."""
    reg = ctx.table_registry()

    corpus_id = "col_xyz"
    reg.register(id="ta", name="ta", source_type="collection", bucket=corpus_id, query_mode="local")
    reg.register(id="tb", name="tb", source_type="collection", bucket=corpus_id, query_mode="local")

    deleted = reg.delete_for_corpus(corpus_id)

    assert set(deleted) == {"ta", "tb"}


def test_delete_for_corpus_empty_corpus_returns_empty_list(ctx):
    """delete_for_corpus() on a corpus with no rows returns []."""
    reg = ctx.table_registry()
    result = reg.delete_for_corpus("col_nonexistent")
    assert result == []


def test_delete_for_corpus_does_not_touch_other_corpora(ctx):
    """Only the target corpus's rows are removed; other collections survive."""
    reg = ctx.table_registry()

    reg.register(id="c1t1", name="c1t1", source_type="collection", bucket="col_1", query_mode="local")
    reg.register(id="c2t1", name="c2t1", source_type="collection", bucket="col_2", query_mode="local")

    reg.delete_for_corpus("col_1")

    assert reg.get("c1t1") is None
    assert reg.get("c2t1") is not None
