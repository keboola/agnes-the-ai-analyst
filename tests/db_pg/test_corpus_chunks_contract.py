"""Cross-engine contract tests for the ``corpus_chunks`` repository.

Parametrises over [DuckDB impl, Postgres impl]. The same calls go to
both backends; the same return shapes must come back.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
import sqlalchemy as sa

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# repo construction helpers
# ---------------------------------------------------------------------------


def _make_duckdb_repo(tmp_path):
    from src.db import _ensure_schema
    from src.repositories.corpus_chunks import CorpusChunksRepository

    conn = duckdb.connect(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    # seed parent corpus + file rows (not FK-constrained in DuckDB, but use
    # real ids for realism)
    conn.execute("INSERT INTO file_corpora (id, slug, name, created_by) VALUES ('col_cc', 'cc', 'CC', 'u')")
    conn.execute(
        "INSERT INTO corpus_files "
        "(id, corpus_id, filename, sha256, file_type) "
        "VALUES ('cf_cc1', 'col_cc', 'doc.txt', 'abc', 'txt')"
    )
    return CorpusChunksRepository(conn), conn


def _make_pg_repo(pg_engine, monkeypatch):
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    with pg_engine.begin() as conn:
        conn.execute(
            sa.text("INSERT INTO file_corpora (id, slug, name, created_by) VALUES (:id, :slug, :name, :by)"),
            {"id": "col_cc", "slug": "cc", "name": "CC", "by": "u"},
        )
        conn.execute(
            sa.text(
                "INSERT INTO corpus_files "
                "(id, corpus_id, filename, sha256, file_type) "
                "VALUES (:id, :corpus_id, :filename, :sha256, :ft)"
            ),
            {
                "id": "cf_cc1",
                "corpus_id": "col_cc",
                "filename": "doc.txt",
                "sha256": "abc",
                "ft": "txt",
            },
        )

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg

    db_pg.dispose()
    db_pg.get_engine()

    from src.repositories.corpus_chunks_pg import CorpusChunksPgRepository

    return CorpusChunksPgRepository(db_pg.get_engine()), None


@pytest.fixture(params=["duckdb", "pg"])
def repo(request, tmp_path, pg_engine, monkeypatch):
    """Yields a corpus_chunks repo bound to either DuckDB or PG."""
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
# contract tests
# ---------------------------------------------------------------------------

CORPUS_ID = "col_cc"
FILE_ID = "cf_cc1"


def test_add_many_then_list_for_file_round_trips(repo):
    chunks = [
        {"corpus_id": CORPUS_ID, "file_id": FILE_ID, "ordinal": 0, "text": "Hello world"},
        {"corpus_id": CORPUS_ID, "file_id": FILE_ID, "ordinal": 1, "text": "Second chunk"},
    ]
    n = repo.add_many(chunks)
    assert n == 2

    rows = repo.list_for_file(FILE_ID)
    assert len(rows) == 2
    texts = [r["text"] for r in rows]
    assert "Hello world" in texts
    assert "Second chunk" in texts


def test_ordinal_ordering_preserved(repo):
    chunks = [
        {"corpus_id": CORPUS_ID, "file_id": FILE_ID, "ordinal": 2, "text": "Third"},
        {"corpus_id": CORPUS_ID, "file_id": FILE_ID, "ordinal": 0, "text": "First"},
        {"corpus_id": CORPUS_ID, "file_id": FILE_ID, "ordinal": 1, "text": "Second"},
    ]
    repo.add_many(chunks)
    rows = repo.list_for_file(FILE_ID)
    assert len(rows) == 3
    assert rows[0]["ordinal"] == 0
    assert rows[1]["ordinal"] == 1
    assert rows[2]["ordinal"] == 2


def test_embedding_column_is_none_on_read(repo):
    repo.add_many([{"corpus_id": CORPUS_ID, "file_id": FILE_ID, "ordinal": 0, "text": "no embed"}])
    rows = repo.list_for_file(FILE_ID)
    assert len(rows) == 1
    assert rows[0]["embedding"] is None


def test_optional_fields_round_trip(repo):
    repo.add_many(
        [
            {
                "corpus_id": CORPUS_ID,
                "file_id": FILE_ID,
                "ordinal": 0,
                "text": "section text",
                "section_path": "Chapter 1 > Intro",
                "page": 3,
                "bbox": "0,0,100,200",
                "metadata": '{"source": "test"}',
            }
        ]
    )
    rows = repo.list_for_file(FILE_ID)
    assert len(rows) == 1
    r = rows[0]
    assert r["section_path"] == "Chapter 1 > Intro"
    assert r["page"] == 3
    assert r["bbox"] == "0,0,100,200"
    assert r["metadata"] == '{"source": "test"}'


def test_list_for_corpus_returns_all_file_chunks(repo):
    repo.add_many(
        [
            {"corpus_id": CORPUS_ID, "file_id": FILE_ID, "ordinal": 0, "text": "chunk A"},
            {"corpus_id": CORPUS_ID, "file_id": FILE_ID, "ordinal": 1, "text": "chunk B"},
        ]
    )
    rows = repo.list_for_corpus(CORPUS_ID)
    assert len(rows) >= 2
    texts = [r["text"] for r in rows]
    assert "chunk A" in texts
    assert "chunk B" in texts


def test_list_for_file_empty_when_no_chunks(repo):
    assert repo.list_for_file("cf_nonexistent") == []


def test_list_for_corpus_empty_when_no_chunks(repo):
    assert repo.list_for_corpus("col_nonexistent") == []


def test_delete_for_file_removes_chunks(repo):
    repo.add_many(
        [
            {"corpus_id": CORPUS_ID, "file_id": FILE_ID, "ordinal": 0, "text": "to remove"},
        ]
    )
    assert len(repo.list_for_file(FILE_ID)) == 1
    repo.delete_for_file(FILE_ID)
    assert repo.list_for_file(FILE_ID) == []


def test_delete_for_file_missing_is_noop(repo):
    # Should not raise
    repo.delete_for_file("cf_nonexistent")


def test_add_many_empty_list_returns_zero(repo):
    n = repo.add_many([])
    assert n == 0


def test_embedding_round_trips(repo):
    vec = [0.01 * i for i in range(384)]
    repo.add_many([{"corpus_id": CORPUS_ID, "file_id": FILE_ID, "ordinal": 0, "text": "v", "embedding": vec}])
    row = repo.list_for_file(FILE_ID)[0]
    stored = row["embedding"]
    assert stored is not None
    assert len(stored) == 384
    assert abs(stored[1] - 0.01) < 1e-6


def test_wrong_dim_embedding_rejected(repo):
    import pytest

    with pytest.raises(Exception):
        repo.add_many(
            [{"corpus_id": CORPUS_ID, "file_id": FILE_ID, "ordinal": 0, "text": "v", "embedding": [0.1, 0.2]}]
        )


def test_list_for_corpora_spans_multiple(repo):
    repo.add_many([{"corpus_id": "col_a", "file_id": "cf_a", "ordinal": 0, "text": "aa"}])
    repo.add_many([{"corpus_id": "col_b", "file_id": "cf_b", "ordinal": 0, "text": "bb"}])
    rows = repo.list_for_corpora(["col_a", "col_b"])
    corpora = {r["corpus_id"] for r in rows}
    assert {"col_a", "col_b"} <= corpora
    assert repo.list_for_corpora([]) == []
