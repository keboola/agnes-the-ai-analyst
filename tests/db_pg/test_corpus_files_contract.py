"""Cross-engine contract tests for the corpus_files repository.

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
    from src.repositories.corpus_files import CorpusFilesRepository

    conn = duckdb.connect(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    # seed a parent corpus row (corpus_files.corpus_id is not FK-constrained
    # in DuckDB but we use a real id for realism)
    conn.execute("INSERT INTO file_corpora (id, slug, name, created_by) VALUES ('col_test', 'test', 'Test', 'u')")
    return CorpusFilesRepository(conn), conn


def _make_pg_repo(pg_engine, monkeypatch):
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    # seed parent corpus
    with pg_engine.begin() as conn:
        conn.execute(
            sa.text("INSERT INTO file_corpora (id, slug, name, created_by) VALUES (:id, :slug, :name, :by)"),
            {"id": "col_test", "slug": "test", "name": "Test", "by": "u"},
        )

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg

    db_pg.dispose()
    db_pg.get_engine()

    from src.repositories.corpus_files_pg import CorpusFilesPgRepository

    return CorpusFilesPgRepository(db_pg.get_engine()), None


@pytest.fixture(params=["duckdb", "pg"])
def repo(request, tmp_path, pg_engine, monkeypatch):
    """Yields a corpus_files repo bound to either DuckDB or PG."""
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

CORPUS_ID = "col_test"


def test_add_then_get_returns_same_shape(repo):
    file_id = repo.add(
        corpus_id=CORPUS_ID,
        filename="report.pdf",
        sha256="abc123",
        file_type="application/pdf",
        size_bytes=1024,
        storage_path="/uploads/report.pdf",
    )
    row = repo.get(file_id)
    assert row is not None
    assert row["id"] == file_id
    assert row["corpus_id"] == CORPUS_ID
    assert row["filename"] == "report.pdf"
    assert row["sha256"] == "abc123"
    assert row["file_type"] == "application/pdf"
    assert row["size_bytes"] == 1024
    assert row["storage_path"] == "/uploads/report.pdf"
    assert row["processing_status"] == "pending"
    assert row["processing_detail"] is None


def test_add_id_has_cf_prefix(repo):
    file_id = repo.add(
        corpus_id=CORPUS_ID,
        filename="x.txt",
        sha256="d",
        file_type=None,
        size_bytes=None,
        storage_path=None,
    )
    assert file_id.startswith("cf_")


def test_add_default_status_is_pending(repo):
    file_id = repo.add(
        corpus_id=CORPUS_ID,
        filename="doc.txt",
        sha256="deadbeef",
        file_type=None,
        size_bytes=None,
        storage_path=None,
    )
    row = repo.get(file_id)
    assert row["processing_status"] == "pending"


def test_add_returns_unique_ids(repo):
    id1 = repo.add(
        corpus_id=CORPUS_ID,
        filename="a.txt",
        sha256="s1",
        file_type=None,
        size_bytes=None,
        storage_path=None,
    )
    id2 = repo.add(
        corpus_id=CORPUS_ID,
        filename="b.txt",
        sha256="s2",
        file_type=None,
        size_bytes=None,
        storage_path=None,
    )
    assert id1 != id2


def test_get_returns_none_when_missing(repo):
    assert repo.get("cf_nonexistent") is None


def test_list_for_corpus_returns_files(repo):
    id1 = repo.add(
        corpus_id=CORPUS_ID,
        filename="x.pdf",
        sha256="h1",
        file_type="application/pdf",
        size_bytes=100,
        storage_path=None,
    )
    id2 = repo.add(
        corpus_id=CORPUS_ID,
        filename="y.pdf",
        sha256="h2",
        file_type="application/pdf",
        size_bytes=200,
        storage_path=None,
    )
    rows = repo.list_for_corpus(CORPUS_ID)
    ids = {r["id"] for r in rows}
    assert {id1, id2} <= ids


def test_list_for_corpus_empty_when_no_files(repo):
    assert repo.list_for_corpus("col_nonexistent") == []


def test_set_status_updates_processing_status(repo):
    file_id = repo.add(
        corpus_id=CORPUS_ID,
        filename="doc.pdf",
        sha256="h3",
        file_type=None,
        size_bytes=None,
        storage_path=None,
    )
    repo.set_status(file_id, status="indexed")
    row = repo.get(file_id)
    assert row["processing_status"] == "indexed"


def test_set_status_with_detail_round_trips_json(repo):
    file_id = repo.add(
        corpus_id=CORPUS_ID,
        filename="big.pdf",
        sha256="h4",
        file_type=None,
        size_bytes=None,
        storage_path=None,
    )
    detail = {"tier": 1, "vision_used": False, "chunk_count": 12}
    repo.set_status(file_id, status="indexed", detail=detail)
    row = repo.get(file_id)
    assert row["processing_status"] == "indexed"
    # detail is stored as JSON text and decoded back to dict on read
    stored = row["processing_detail"]
    assert isinstance(stored, dict), f"Expected dict, got {type(stored)}: {stored!r}"
    assert stored["chunk_count"] == 12
    assert stored["tier"] == 1


def test_set_status_rejected_with_error_detail(repo):
    file_id = repo.add(
        corpus_id=CORPUS_ID,
        filename="broken.pdf",
        sha256="h5",
        file_type=None,
        size_bytes=None,
        storage_path=None,
    )
    repo.set_status(file_id, status="rejected", detail={"error": "parse failed"})
    row = repo.get(file_id)
    assert row["processing_status"] == "rejected"
    assert row["processing_detail"]["error"] == "parse failed"
