"""DuckDB FTS BM25 search over knowledge_items (issue #121).

Covers:
- BM25 ranking — multi-term query returns results scored by relevance,
  not by ``updated_at``.
- ILIKE fallback path — when ``ensure_fts_loaded`` returns False
  (extension blocked / network sandboxed), the legacy ILIKE behaviour
  still runs and returns the full match set (just unranked).
- Index rebuild after mutation — newly created items show up in BM25
  results in the same connection.
- Czech-diacritic handling — ``strip_accents=1`` lets ``cesky`` match
  documents containing ``česky``.
"""

from __future__ import annotations

from unittest.mock import patch

import duckdb
import pytest


def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import src.db as db_module

    db_module._system_db_conn = None
    db_module._system_db_path = None
    return db_module.get_system_db()


class TestBM25Ranking:
    def test_multi_term_query_orders_by_relevance(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository

        repo = KnowledgeRepository(conn)
        # Item A: title hits twice (review + review), short content.
        repo.create(id="a", title="review review", content="x", category="x")
        # Item B: one title hit, one content hit.
        repo.create(id="b", title="review", content="this is a review item", category="x")
        # Item C: no hit at all.
        repo.create(id="c", title="unrelated", content="nothing here", category="x")

        results = repo.search("review", limit=10)
        # Both A and B match; C must be filtered out by the BM25 IS NOT NULL.
        ids = [r["id"] for r in results]
        assert "c" not in ids
        assert set(ids) == {"a", "b"}
        # Insertion order is A then B. ILIKE fallback would return them by
        # ``updated_at DESC`` → B before A. BM25 picks the higher-density
        # match → A before B. Either ordering is acceptable here (depends
        # on whether the FTS extension loaded); we just assert that the
        # match set is correct. The next test guards BM25-specific shape.

    def test_bm25_score_attached_when_extension_available(self, tmp_path, monkeypatch):
        """If FTS is available, results carry a ``bm25_score`` column.

        Lets the API surface relevance scores to the UI later (#121
        out-of-scope but tracked) without a separate query path.
        """
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.fts import ensure_fts_loaded
        from src.repositories.knowledge import KnowledgeRepository

        if not ensure_fts_loaded(conn):
            pytest.skip("fts extension not loadable in this environment")

        repo = KnowledgeRepository(conn)
        repo.create(id="a", title="release process", content="", category="x")
        repo.create(id="b", title="release cut", content="re-read live docs", category="x")
        results = repo.search("release", limit=10)
        assert len(results) == 2
        for r in results:
            assert "bm25_score" in r
            assert r["bm25_score"] is not None
            assert r["bm25_score"] > 0

    def test_higher_density_match_ranks_first_when_fts_available(self, tmp_path, monkeypatch):
        """Adversarial-review fix: the previous ``test_multi_term_query_orders_by_relevance``
        explicitly asserted only the match-set, not the ordering — which
        left BM25 *ranking* untested even though it's the headline #121
        feature. This test pins ordering when the extension is loaded:
        Item A (term hits twice in a short title) MUST rank above Item B
        (term hits twice across a longer title+content). When FTS is
        unavailable we ``skip`` rather than ``xfail`` — the ILIKE
        fallback's ``updated_at DESC`` ordering is an intentionally
        different contract.
        """
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.fts import ensure_fts_loaded
        from src.repositories.knowledge import KnowledgeRepository

        if not ensure_fts_loaded(conn):
            pytest.skip("fts extension not loadable in this environment")

        repo = KnowledgeRepository(conn)
        repo.create(id="a", title="review review", content="x", category="x")
        repo.create(id="b", title="review", content="this is a review item", category="x")
        results = repo.search("review", limit=10)
        ids = [r["id"] for r in results]
        assert set(ids) == {"a", "b"}
        # Higher term density in A's tiny title-only body → higher BM25
        # score → A precedes B in the result list.
        assert ids.index("a") < ids.index("b"), (
            f"expected A before B by BM25 density; got {ids}"
        )


class TestILIKEFallback:
    def test_falls_back_to_ilike_when_fts_unavailable(self, tmp_path, monkeypatch):
        """``ensure_fts_loaded`` returning False routes through the legacy
        ILIKE path — same predicate, ORDER BY ``updated_at`` DESC.
        """
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository

        repo = KnowledgeRepository(conn)
        repo.create(id="a", title="release one", content="", category="x")
        repo.create(id="b", title="release two", content="", category="x")
        repo.create(id="c", title="nothing", content="", category="x")

        # Force the fallback path even if FTS is installed locally.
        with patch("src.fts.ensure_fts_loaded", return_value=False):
            results = repo.search("release", limit=10)

        assert len(results) > 0, "fallback search returned empty before ordering check"
        ids = [r["id"] for r in results]
        assert "c" not in ids
        assert set(ids) == {"a", "b"}
        # Adversarial-review fix: the ILIKE branch now selects an
        # explicit ``NULL AS bm25_score`` so the result-column shape
        # matches the FTS path. Consumers can read the score uniformly;
        # the absence of relevance ranking is signalled by the column
        # being ``None`` everywhere, not by the column being missing.
        assert "bm25_score" in results[0]
        assert results[0]["bm25_score"] is None

    def test_count_items_falls_back_to_ilike_when_fts_unavailable(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository

        repo = KnowledgeRepository(conn)
        repo.create(id="a", title="release one", content="", category="x")
        repo.create(id="b", title="release two", content="", category="x")
        repo.create(id="c", title="nothing", content="", category="x")

        with patch("src.fts.ensure_fts_loaded", return_value=False):
            total = repo.count_items(search="release")
        assert total == 2

    def test_search_falls_back_when_index_missing_despite_extension_loaded(
        self, tmp_path, monkeypatch
    ):
        """Adversarial-review fix: ``ensure_fts_loaded`` returning True
        only guarantees the extension is loadable, NOT that the
        ``fts_main_knowledge_items`` index exists. Migration soft-fail,
        a concurrent ``overwrite=1`` rebuild's drop-then-create window,
        or a manual `DROP INDEX` (test fixtures sometimes do this) all
        leave the extension loaded but the index missing. ``search()``
        MUST catch ``duckdb.Error`` from the BM25 query and fall through
        to ILIKE, not 500 the /api/memory?search= endpoint.
        """
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.fts import ensure_fts_loaded
        from src.repositories.knowledge import KnowledgeRepository

        if not ensure_fts_loaded(conn):
            pytest.skip("fts extension not loadable in this environment")

        repo = KnowledgeRepository(conn)
        repo.create(id="a", title="release alpha", content="", category="x")
        repo.create(id="b", title="release beta", content="", category="x")
        repo.create(id="c", title="nothing", content="", category="x")

        # Drop the FTS index out from under the repo. ``ensure_fts_loaded``
        # will still return True (extension loaded), but the next BM25
        # query raises a Catalog Error — the exact production failure
        # mode this fallback guards against.
        try:
            conn.execute("PRAGMA drop_fts_index('main.knowledge_items')")
        except Exception:
            pytest.skip("drop_fts_index PRAGMA unavailable; cannot simulate missing-index path")

        # search() must NOT raise — it must transparently fall through
        # to ILIKE.
        results = repo.search("release", limit=10)
        ids = [r["id"] for r in results]
        assert set(ids) == {"a", "b"}, f"expected ILIKE fallback to find a + b; got {ids}"

        # count_items() honors the same fallback.
        total = repo.count_items(search="release")
        assert total == 2


class TestIndexRebuildOnMutation:
    def test_new_item_is_searchable_immediately(self, tmp_path, monkeypatch):
        """``create()`` rebuilds the FTS index; the new item must surface
        on the next ``search()`` against its terms.
        """
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.fts import ensure_fts_loaded
        from src.repositories.knowledge import KnowledgeRepository

        if not ensure_fts_loaded(conn):
            pytest.skip("fts extension not loadable in this environment")

        repo = KnowledgeRepository(conn)
        # No items yet → empty result.
        assert repo.search("singleton") == []

        repo.create(id="new", title="singleton token", content="rare", category="x")
        results = repo.search("singleton")
        assert len(results) == 1
        assert results[0]["id"] == "new"

    def test_title_update_resurfaces_under_new_term(self, tmp_path, monkeypatch):
        """Updating the title rebuilds the index so the row matches the
        new term and stops matching the old one.
        """
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.fts import ensure_fts_loaded
        from src.repositories.knowledge import KnowledgeRepository

        if not ensure_fts_loaded(conn):
            pytest.skip("fts extension not loadable in this environment")

        repo = KnowledgeRepository(conn)
        repo.create(id="r", title="alpha bravo", content="", category="x")
        assert [r["id"] for r in repo.search("alpha")] == ["r"]

        repo.update("r", title="charlie delta")
        # Old term no longer matches.
        assert repo.search("alpha") == []
        # New term matches.
        assert [r["id"] for r in repo.search("charlie")] == ["r"]


class TestCzechDiacritics:
    def test_query_without_diacritics_matches_indexed_diacritics(self, tmp_path, monkeypatch):
        """``strip_accents=1`` lets a query of ``cesky`` match a doc
        containing ``česky``. This is the requirement from issue #121.
        """
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.fts import ensure_fts_loaded
        from src.repositories.knowledge import KnowledgeRepository

        if not ensure_fts_loaded(conn):
            pytest.skip("fts extension not loadable in this environment")

        repo = KnowledgeRepository(conn)
        repo.create(
            id="cs",
            title="česky preferuje",
            content="user prefers Czech locale; česká diakritika test",
            category="x",
        )
        # Accent-stripped query must still hit the diacritic-bearing doc.
        results = repo.search("cesky")
        assert [r["id"] for r in results] == ["cs"]
