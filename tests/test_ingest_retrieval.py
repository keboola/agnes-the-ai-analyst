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


def _seed_files(slug: str, files: list[tuple[str, list[dict]]]) -> str:
    """Seed several *files* (not just chunks) under one corpus, so tests can
    control the corpus's distinct-file count."""
    from src.repositories import corpus_chunks_repo, corpus_files_repo, file_corpora_repo

    cid = file_corpora_repo().create(name=slug, slug=slug, description=None, created_by="u")
    for filename, chunks in files:
        fid = corpus_files_repo().add(
            corpus_id=cid,
            filename=filename,
            sha256="s",
            file_type="txt",
            size_bytes=1,
            storage_path="/x",
        )
        rows = [{"corpus_id": cid, "file_id": fid, **c} for c in chunks]
        corpus_chunks_repo().add_many(rows)
    return cid


def test_search_idf_ranks_distinctive_term_over_common_term(e2e_env):
    """#756: a chunk matching only a term that's rare across the candidate
    set must outrank a chunk matching only a term common to most candidates
    — even though both match exactly one of the two query terms (a tie
    under the old "fraction of distinct terms present" score)."""
    from src.ingest.retrieval import search

    cid = _seed_files(
        "rs-idf",
        [
            ("kube.txt", [{"ordinal": 0, "text": "kubernetes cluster autoscaling guide"}]),
            ("data1.txt", [{"ordinal": 0, "text": "data warehouse pipeline overview"}]),
            ("data2.txt", [{"ordinal": 0, "text": "data quality checks nightly"}]),
            ("data3.txt", [{"ordinal": 0, "text": "data retention policy"}]),
            ("data4.txt", [{"ordinal": 0, "text": "data export formats"}]),
        ],
    )
    res = search([cid], "data kubernetes")
    assert res
    # "kubernetes" is unique to kube.txt (high IDF); "data" is common to the
    # other four files (low IDF) — the distinctive-term match must win.
    assert res[0]["filename"] == "kube.txt"
    assert res[0]["score"] > res[1]["score"]


def test_search_tiny_corpus_small_margin_is_low_confidence(e2e_env):
    """#756: on a tiny corpus (2 files) with a tied/near-tied top score, the
    surfaced confidence must be "low" — never presented as a trustworthy
    top pick."""
    from src.ingest.retrieval import search

    cid = _seed_files(
        "rs-tiny",
        [
            ("a.txt", [{"ordinal": 0, "text": "shared keyword apple"}]),
            ("b.txt", [{"ordinal": 0, "text": "shared keyword apple"}]),
        ],
    )
    res = search([cid], "apple")
    assert res
    assert all(r["confidence"] == "low" for r in res)


def test_search_embeddings_absent_tie_is_deterministic(e2e_env, monkeypatch):
    """#756: with embeddings absent (the default deployment), a lexical-score
    tie must resolve deterministically (stable chunk-id tie-break) instead
    of by arbitrary DB fetch order."""
    import src.ingest.retrieval as retrieval
    from src.repositories import corpus_chunks_repo

    monkeypatch.setattr(retrieval, "embed_query", lambda q: None)
    cid = _seed_files(
        "rs-det",
        [
            ("z.txt", [{"ordinal": 0, "text": "shared keyword apple"}]),
            ("a.txt", [{"ordinal": 0, "text": "shared keyword apple"}]),
        ],
    )
    chunk_ids = sorted(c["id"] for c in corpus_chunks_repo().list_for_corpus(cid))

    res1 = retrieval.search([cid], "apple")
    res2 = retrieval.search([cid], "apple")

    assert [r["chunk_id"] for r in res1] == chunk_ids
    assert [r["chunk_id"] for r in res2] == chunk_ids


def test_search_normalization_handles_single_candidate(e2e_env):
    """#756: min-max normalization over a single-candidate set (or a set
    with no lexical/vector signal) must not divide by zero."""
    from src.ingest.retrieval import search

    cid = _seed("rs-single", [{"ordinal": 0, "text": "hello world"}])
    res = search([cid], "hello")
    assert res
    assert res[0]["score"] > 0
    assert res[0]["confidence"] == "low"  # a single-file corpus can't discriminate
