"""Cross-engine contract tests for the glossary_terms repository."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _make_duckdb_repo(tmp_path):
    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb
    from src.repositories.glossary import GlossaryRepository

    conn = _open_duckdb(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return GlossaryRepository(conn), conn


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

    from src.repositories.glossary_pg import GlossaryPgRepository

    return GlossaryPgRepository(db_pg.get_engine()), None


@pytest.fixture(params=["duckdb", "pg"])
def repo(request, tmp_path, pg_engine, monkeypatch):
    if request.param == "duckdb":
        r, conn = _make_duckdb_repo(tmp_path)
        yield r
        conn.close()
    else:
        r, _ = _make_pg_repo(pg_engine, monkeypatch)
        yield r


def test_create_then_get_roundtrip(repo):
    assert repo.get("kb/model-1/mrr") is None
    row = repo.create(
        id="kb/model-1/mrr",
        term="Monthly Recurring Revenue",
        definition="Revenue normalized to a monthly cadence.",
        see_also=["arr", "churn"],
        model_uuid="model-1",
        source="keboola_semantic_layer",
    )
    assert row["term"] == "Monthly Recurring Revenue"
    assert row["see_also"] == ["arr", "churn"]
    assert row["source"] == "keboola_semantic_layer"

    fetched = repo.get("kb/model-1/mrr")
    assert fetched["definition"] == "Revenue normalized to a monthly cadence."


def test_create_with_empty_see_also_roundtrips(repo):
    """Regression: ``see_also=[]`` (the common case — a term the Keboola
    importer sees with no seeAlso list) binds to a Postgres
    ``character varying[]`` column via psycopg3 with no explicit type, which
    can raise 'cannot determine type of empty array' if the driver can't
    infer an OID. Exercises both backends via the parametrized ``repo``
    fixture."""
    row = repo.create(
        id="kb/model-1/no_see_also",
        term="Standalone Term",
        definition="A term with no related terms.",
        see_also=[],
    )
    assert row["see_also"] == []

    fetched = repo.get("kb/model-1/no_see_also")
    assert fetched["see_also"] == []


def test_create_upserts_on_conflict(repo):
    repo.create(id="kb/m/x", term="X", definition="first")
    repo.create(id="kb/m/x", term="X", definition="second")
    assert repo.get("kb/m/x")["definition"] == "second"


def test_list_orders_by_term(repo):
    repo.create(id="a", term="Zeta", definition="z")
    repo.create(id="b", term="Alpha", definition="a")
    terms = [r["term"] for r in repo.list()]
    assert terms == ["Alpha", "Zeta"]


def test_delete_removes_row(repo):
    repo.create(id="kb/m/x", term="X", definition="d")
    assert repo.delete("kb/m/x") is True
    assert repo.get("kb/m/x") is None
    assert repo.delete("kb/m/x") is False


def test_search_ilike_matches_term_or_definition(repo):
    repo.create(id="a", term="Churn Rate", definition="Percent of customers lost.")
    repo.create(id="b", term="Retention", definition="Opposite of churn.")
    repo.create(id="c", term="Unrelated", definition="Nothing to do with the query.")

    results = repo.search("churn")
    ids = {r["id"] for r in results}
    assert ids == {"a", "b"}


def test_search_ranks_by_bm25_when_fts_available(tmp_path):
    """DuckDB-only: BM25 should rank an exact-term match above a
    definition-only mention, even when alphabetical order disagrees."""
    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb
    from src.repositories.glossary import GlossaryRepository

    conn = _open_duckdb(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    repo = GlossaryRepository(conn)

    repo.create(id="a", term="Aardvark Metric", definition="Mentions churn in passing.")
    repo.create(id="b", term="Churn Rate", definition="The core definition of churn.")

    results = repo.search("churn")
    ids = [r["id"] for r in results]
    assert ids[0] == "b"  # term match ranks first under BM25, despite "Aardvark" < "Churn" alphabetically
    conn.close()


def test_search_ranks_by_ts_rank_on_pg(pg_engine, monkeypatch):
    """PG-only counterpart to test_search_ranks_by_bm25_when_fts_available:
    ``ts_rank`` should rank an exact-term match above a definition-only
    mention, even when alphabetical order disagrees."""
    repo, _ = _make_pg_repo(pg_engine, monkeypatch)

    repo.create(id="a", term="Aardvark Metric", definition="Mentions churn in passing.")
    repo.create(id="b", term="Churn Rate", definition="The core definition of churn.")

    results = repo.search("churn")
    ids = [r["id"] for r in results]
    assert ids[0] == "b"  # term match ranks first under ts_rank, despite "Aardvark" < "Churn" alphabetically
    assert "bm25_score" in results[0]
    assert results[0]["bm25_score"] is not None
