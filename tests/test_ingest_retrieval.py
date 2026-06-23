"""Tests for src.ingest.retrieval.search — hybrid + fail-closed RBAC scoping."""

from __future__ import annotations


def _seed(slug: str, chunks: list[dict]) -> str:
    from src.repositories import corpus_chunks_repo, corpus_files_repo, file_corpora_repo

    cid = file_corpora_repo().create(name=slug, slug=slug, description=None, created_by="u")
    fid = corpus_files_repo().add(
        corpus_id=cid,
        filename=f"{slug}.txt",
        sha256="s",
        file_type="txt",
        size_bytes=1,
        storage_path="/x",
    )
    rows = [{"corpus_id": cid, "file_id": fid, **c} for c in chunks]
    corpus_chunks_repo().add_many(rows)
    return cid


def test_search_fail_closed_on_empty_inputs(e2e_env):
    from src.ingest.retrieval import search

    assert search([], "anything") == []
    cid = _seed("rs-empty", [{"ordinal": 0, "text": "hello world"}])
    assert search([cid], "   ") == []


def test_search_lexical_ranks_matches(e2e_env):
    from src.ingest.retrieval import search

    cid = _seed(
        "rs-lex",
        [
            {"ordinal": 0, "text": "the quick brown fox jumps over"},
            {"ordinal": 1, "text": "completely unrelated weather report"},
        ],
    )
    res = search([cid], "brown fox")
    assert res
    assert res[0]["text"].startswith("the quick brown fox")
    assert res[0]["filename"] == "rs-lex.txt"
    assert res[0]["score"] > 0
    assert res[0]["chunk_id"]


def test_search_is_rbac_scoped_to_listed_corpora(e2e_env):
    from src.ingest.retrieval import search

    cid_a = _seed("rs-a", [{"ordinal": 0, "text": "shared keyword apple"}])
    _seed("rs-b", [{"ordinal": 0, "text": "shared keyword apple"}])
    # Only corpus A is granted → corpus B must never appear (fail-closed).
    res = search([cid_a], "apple")
    assert res
    assert all(r["corpus_id"] == cid_a for r in res)


def test_search_hybrid_uses_embeddings(e2e_env, monkeypatch):
    import src.ingest.retrieval as retrieval
    from src.ingest.retrieval import search

    # Query vector aligned with the first chunk's embedding; no lexical overlap.
    monkeypatch.setattr(retrieval, "embed_query", lambda q: [1.0] + [0.0] * 383)
    cid = _seed(
        "rs-vec",
        [
            {"ordinal": 0, "text": "alpha beta gamma", "embedding": [1.0] + [0.0] * 383},
            {"ordinal": 1, "text": "delta epsilon zeta", "embedding": [0.0] * 384},
        ],
    )
    res = search([cid], "no-lexical-overlap-query")
    assert res
    assert res[0]["ordinal"] == 0  # cosine picked the aligned vector
