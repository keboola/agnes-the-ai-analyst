"""Cross-engine contract tests for the knowledge_digests repository (K4, #799).

Parametrises over [DuckDB impl, Postgres impl]. The same calls go to both
backends; the same return shapes must come back.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# repo construction helpers
# ---------------------------------------------------------------------------


def _make_duckdb_repo(tmp_path):
    from src.db import _ensure_schema
    from src.repositories.knowledge_digests import KnowledgeDigestsRepository

    conn = duckdb.connect(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return KnowledgeDigestsRepository(conn), conn


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

    from src.repositories.knowledge_digests_pg import KnowledgeDigestsPgRepository

    return KnowledgeDigestsPgRepository(db_pg.get_engine()), None


@pytest.fixture(params=["duckdb", "pg"])
def repo(request, tmp_path, pg_engine, monkeypatch):
    """Yields a knowledge_digests repo bound to either DuckDB or PG."""
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


def test_create_get_roundtrip_decodes_corpus_ids(repo):
    did = repo.create(
        slug="arch",
        title="Architecture overview",
        instructions="Maintain an overview of our architecture.",
        source_corpus_ids=["col_a", "col_b"],
        created_by="u1",
    )
    assert did.startswith("kd_")
    row = repo.get(did)
    assert row is not None
    assert row["slug"] == "arch" and row["status"] == "pending"
    assert row["source_corpus_ids"] == ["col_a", "col_b"]
    assert row["output_md"] is None and row["generated_at"] is None
    assert repo.get_by_slug("arch")["id"] == did


def test_create_returns_unique_ids(repo):
    id1 = repo.create(
        slug="one",
        title="One",
        instructions="i",
        source_corpus_ids=[],
        created_by="u",
    )
    id2 = repo.create(
        slug="two",
        title="Two",
        instructions="i",
        source_corpus_ids=[],
        created_by="u",
    )
    assert id1 != id2


def test_get_returns_none_when_missing(repo):
    assert repo.get("kd_nonexistent") is None


def test_get_by_slug_returns_none_when_missing(repo):
    assert repo.get_by_slug("no-such-slug") is None


def test_slug_unique(repo):
    repo.create(slug="dup", title="A", instructions="i", source_corpus_ids=[], created_by="u")
    with pytest.raises(Exception):
        repo.create(slug="dup", title="B", instructions="i", source_corpus_ids=[], created_by="u")


def test_list_orders_by_created_at(repo):
    id1 = repo.create(slug="l1", title="L1", instructions="i", source_corpus_ids=[], created_by="u")
    id2 = repo.create(slug="l2", title="L2", instructions="i", source_corpus_ids=[], created_by="u")
    rows = repo.list()
    ids = [r["id"] for r in rows]
    assert ids.index(id1) < ids.index(id2)


def test_update_edits_fields_not_slug(repo):
    did = repo.create(
        slug="edit-me",
        title="Old title",
        instructions="old instructions",
        source_corpus_ids=["col_a"],
        created_by="u",
    )
    before = repo.get(did)
    repo.update(
        did,
        title="New title",
        instructions="new instructions",
        source_corpus_ids=["col_c"],
    )
    after = repo.get(did)
    assert after["title"] == "New title"
    assert after["instructions"] == "new instructions"
    assert after["source_corpus_ids"] == ["col_c"]
    assert after["slug"] == "edit-me"  # slug is immutable
    assert after["updated_at"] >= before["updated_at"]


def test_update_all_none_is_noop(repo):
    did = repo.create(
        slug="noop",
        title="Title",
        instructions="instructions",
        source_corpus_ids=["col_a"],
        created_by="u",
    )
    before = repo.get(did)
    repo.update(did)  # all-None — must not raise
    after = repo.get(did)
    assert after["title"] == before["title"]
    assert after["instructions"] == before["instructions"]
    assert after["source_corpus_ids"] == before["source_corpus_ids"]


def test_set_generated_is_atomic_fresh(repo):
    did = repo.create(
        slug="gen",
        title="Gen",
        instructions="i",
        source_corpus_ids=["col_a"],
        created_by="u",
    )
    repo.set_generated(did, output_md="# D", source_fingerprint="fp1", model="claude-test")
    row = repo.get(did)
    assert row["status"] == "fresh"
    assert row["status_reason"] is None
    assert row["output_md"] == "# D"
    assert row["source_fingerprint"] == "fp1"
    assert row["model"] == "claude-test"
    assert row["generated_at"] is not None


def test_mark_stale_keeps_previous_output(repo):
    did = repo.create(
        slug="stale-me",
        title="Stale",
        instructions="i",
        source_corpus_ids=["col_a"],
        created_by="u",
    )
    repo.set_generated(did, output_md="# D", source_fingerprint="fp1", model="claude-test")
    generated_at_before = repo.get(did)["generated_at"]

    repo.mark_stale(did, reason="LLM timeout")
    row = repo.get(did)
    assert row["status"] == "stale"
    assert row["status_reason"] == "LLM timeout"
    # previous generation output survives untouched
    assert row["output_md"] == "# D"
    assert row["source_fingerprint"] == "fp1"
    assert row["generated_at"] == generated_at_before


def test_set_generated_clears_previous_stale_reason(repo):
    did = repo.create(
        slug="recover",
        title="Recover",
        instructions="i",
        source_corpus_ids=["col_a"],
        created_by="u",
    )
    repo.mark_stale(did, reason="LLM not configured")
    repo.set_generated(did, output_md="# fixed", source_fingerprint="fp2", model="claude-test")
    row = repo.get(did)
    assert row["status"] == "fresh"
    assert row["status_reason"] is None
    assert row["output_md"] == "# fixed"


def test_delete_and_list(repo):
    did = repo.create(slug="gone", title="Gone", instructions="i", source_corpus_ids=[], created_by="u")
    assert repo.get(did) is not None
    repo.delete(did)
    assert repo.get(did) is None
    assert did not in {r["id"] for r in repo.list()}
