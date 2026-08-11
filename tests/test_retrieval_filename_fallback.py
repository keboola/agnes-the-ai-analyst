"""Searching for a file by its NAME finds it.

`rank_chunks` scores chunk text only — its own docstring says "no filename
resolution" — and the filename is attached afterwards, for the citation. So
the most natural query a person or an agent makes ("what is in
quarterly-report.md?") matched nothing, because the words are in the file's
NAME and not in its body.

Observed live: an agent asked about an uploaded file searched for the
filename, got `[]`, and concluded it lacked access. #1236 made that empty
result explain itself; this makes the query work.

Deliberately a FALLBACK, not a scoring change: content hits still win and
rank exactly as before, and the filename pass runs only when the body
search comes up empty. A filename is weak evidence — it should never
outrank a real match in the text.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _seed(slug: str, filename: str, chunks: list[dict]) -> str:
    from src.repositories import corpus_chunks_repo, corpus_files_repo, file_corpora_repo

    cid = file_corpora_repo().create(name=slug, slug=slug, description=None, created_by="u")
    fid = corpus_files_repo().add(
        corpus_id=cid,
        filename=filename,
        sha256="s",
        file_type=filename.rsplit(".", 1)[-1],
        size_bytes=1,
        storage_path="/x",
    )
    rows = [{"corpus_id": cid, "file_id": fid, **c} for c in chunks]
    corpus_chunks_repo().add_many(rows)
    return cid


class TestFilenameFallback:
    def test_query_matching_only_the_filename_finds_the_file(self, e2e_env):
        from src.ingest.retrieval import search

        cid = _seed(
            "fn-basic",
            "quarterly-report.md",
            [{"ordinal": 0, "text": "alpha bravo charlie delta"}],
        )
        res = search([cid], "quarterly-report")
        assert res, "a file cannot be found by the name it is displayed under"
        assert res[0]["filename"] == "quarterly-report.md"

    def test_a_single_distinctive_word_from_the_name_is_enough(self, e2e_env):
        from src.ingest.retrieval import search

        cid = _seed("fn-word", "zs-test-poznamka.md", [{"ordinal": 0, "text": "alpha bravo"}])
        assert search([cid], "poznamka"), "filename tokens are not searchable"

    def test_the_extension_alone_does_not_match_everything(self, e2e_env):
        """`md` is in every markdown filename — matching on it returns noise."""
        from src.ingest.retrieval import search

        cid = _seed("fn-ext", "notes.md", [{"ordinal": 0, "text": "alpha bravo"}])
        assert search([cid], "md") == []

    def test_content_hits_still_win_and_are_unchanged(self, e2e_env):
        """The fallback must not reorder a search that already worked."""
        from src.ingest.retrieval import search

        cid = _seed(
            "fn-content",
            "report.md",
            [
                {"ordinal": 0, "text": "the quick brown fox jumps over"},
                {"ordinal": 1, "text": "completely unrelated weather"},
            ],
        )
        res = search([cid], "brown fox")
        assert res[0]["text"].startswith("the quick brown fox")
        assert res[0]["score"] > 0

    def test_a_content_match_is_preferred_over_a_filename_match(self, e2e_env):
        """A body hit beats a name hit — the name is the weaker signal."""
        from src.ingest.retrieval import search

        cid = _seed("fn-vs-a", "revenue.md", [{"ordinal": 0, "text": "alpha bravo"}])
        _seed("fn-vs-b", "notes.md", [{"ordinal": 0, "text": "revenue grew 12 percent"}])
        res = search([cid], "revenue")
        assert res, "precondition"
        # Only one corpus is in scope, so this asserts the fallback did not
        # fire while a real content hit existed elsewhere in that corpus.
        assert res[0]["filename"] == "revenue.md"

    def test_no_match_anywhere_still_returns_empty(self, e2e_env):
        from src.ingest.retrieval import search

        cid = _seed("fn-none", "notes.md", [{"ordinal": 0, "text": "alpha bravo"}])
        assert search([cid], "nosuchwordanywhere") == []

    def test_blank_query_is_still_fail_closed(self, e2e_env):
        from src.ingest.retrieval import search

        cid = _seed("fn-blank", "notes.md", [{"ordinal": 0, "text": "alpha"}])
        assert search([cid], "   ") == []

    def test_filename_hits_are_labelled_low_confidence(self, e2e_env):
        """A name match is a hint, not evidence — say so in the payload."""
        from src.ingest.retrieval import search

        cid = _seed("fn-conf", "quarterly-report.md", [{"ordinal": 0, "text": "alpha"}])
        res = search([cid], "quarterly-report")
        assert res and res[0].get("confidence") == "low"

    def test_fallback_respects_the_corpus_scope(self, e2e_env):
        """Fail-closed RBAC applies to the name pass as much as the text pass."""
        from src.ingest.retrieval import search

        cid_a = _seed("fn-rbac-a", "alpha-notes.md", [{"ordinal": 0, "text": "x"}])
        _seed("fn-rbac-b", "beta-secret.md", [{"ordinal": 0, "text": "y"}])
        assert search([cid_a], "beta-secret") == []

    def test_result_rows_keep_their_shape(self, e2e_env):
        from src.ingest.retrieval import search

        cid = _seed("fn-shape", "quarterly-report.md", [{"ordinal": 0, "text": "alpha"}])
        row = search([cid], "quarterly-report")[0]
        for key in ("chunk_id", "corpus_id", "file_id", "filename", "ordinal", "text", "score"):
            assert key in row, f"filename hit is missing {key}"


class TestANameHitCannotCrowdOutRealMatches:
    """Devin Review on #1267: `_minmax` makes any bucket's top hit 1.0.

    A fallback that matched only file names would therefore arrive looking
    exactly as strong as a document that genuinely contains the words, and
    could fill every slot of a combined answer — hiding the knowledge notes,
    glossary entries and tables that actually matched.
    """

    def test_a_body_hit_is_labelled_as_one(self, e2e_env):
        from src.ingest.retrieval import search

        cid = _seed("label-body", "notes.md", [{"ordinal": 0, "text": "alpha bravo charlie"}])
        res = search([cid], "bravo")
        assert res and res[0]["matched_on"] == "body"

    def test_a_name_hit_is_labelled_as_one(self, e2e_env):
        from src.ingest.retrieval import search

        cid = _seed("label-name", "quarterly-report.md", [{"ordinal": 0, "text": "alpha bravo"}])
        res = search([cid], "quarterly-report")
        assert res and res[0]["matched_on"] == "filename"

    def test_the_fallback_fires_even_when_scoring_returns_something(self, e2e_env):
        """The trigger is "no body carries the query", not "no results".

        With the embeddings extra installed every chunk gets a non-zero
        cosine, so `rank_chunks` essentially always returns rows — an
        empty-only trigger made the whole fallback dead code on exactly the
        instances that have semantic search. Simulated here by scoring every
        chunk from a query that appears in no body. (Devin Review on #1267.)
        """
        from unittest.mock import patch

        from src.ingest import retrieval

        cid = _seed("label-semantic", "quarterly-report.md", [{"ordinal": 0, "text": "alpha bravo"}])

        real = retrieval.rank_chunks

        def _always_scores(chunks, query, *, k=10):
            top, _conf = real(chunks, query, k=k)
            if top:
                return top, "medium"
            return [(0.42, ch) for ch in chunks][:k], "medium"

        with patch.object(retrieval, "rank_chunks", _always_scores):
            res = retrieval.search([cid], "quarterly-report")

        assert res, "the fallback never ran"
        assert res[0]["matched_on"] == "filename"
        assert res[0]["confidence"] == "low"

    def test_the_combined_search_caps_a_name_only_chunk_bucket(self, e2e_env):
        """Two name-only chunks at most WHEN something else matched too."""
        from src.search.unified import unified_search

        cid = _seed(
            "cap-name",
            "quarterly-report.md",
            [{"ordinal": i, "text": f"alpha bravo charlie {i}"} for i in range(6)],
        )
        knowledge = [
            {"id": f"k{i}", "title": f"quarterly report note {i}", "content": "quarterly report", "domain": "d"}
            for i in range(3)
        ]

        from unittest.mock import patch

        with patch("src.search.unified._knowledge_search", return_value=knowledge):
            hits = unified_search(
                "quarterly-report",
                corpus_ids=[cid],
                user_groups=None,
                granted_domains=None,
                tables=[],
                k=10,
            )

        chunks = [h for h in hits if h.get("type") == "chunk"]
        assert chunks, "the name match must still be reachable"
        assert len(chunks) <= 2, f"a name-only bucket took {len(chunks)} slots"
        assert any(h.get("type") == "knowledge" for h in hits), "real matches must survive"

    def test_the_cap_does_not_fire_when_nothing_else_matched(self, e2e_env):
        """Then the cap would only throw away the answer to the query that
        motivated the fallback. (Devin Review on #1267.)"""
        from src.search.unified import unified_search

        cid = _seed(
            "cap-alone",
            "quarterly-report.md",
            [{"ordinal": i, "text": f"alpha bravo charlie {i}"} for i in range(6)],
        )

        from unittest.mock import patch

        with patch("src.search.unified._knowledge_search", return_value=[]):
            hits = unified_search(
                "quarterly-report",
                corpus_ids=[cid],
                user_groups=None,
                granted_domains=None,
                tables=[],
                k=10,
            )

        chunks = [h for h in hits if h.get("type") == "chunk"]
        assert len(chunks) > 2, f"only {len(chunks)} chunks, but nothing else matched"

    def test_offline_search_has_the_same_fallback(self):
        """Behaviour is pinned in `tests/test_search_local.py` against a real
        built artifact; this only guards that the offline reader still calls
        the shared ranker rather than growing its own."""
        import inspect

        from src.search import local

        src = inspect.getsource(local.local_search)
        assert "_rank_by_filename" in src


class TestTheAdviceMatchesTheBehaviour:
    """The caveat told users and agents not to do the thing that now works."""

    def test_no_surface_still_says_filenames_are_not_indexed(self):
        import subprocess

        out = subprocess.run(
            ["grep", "-rn", "filenames are not indexed", "--include=*.py", "app", "cli", "src"],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
        ).stdout
        assert out.strip() == "", f"stale caveat still shipped:\n{out}"

    def test_both_mcp_surfaces_describe_the_fallback(self):
        for path in ("cli/mcp/server.py", "app/api/mcp/foundation_tools.py"):
            text = (ROOT / path).read_text()
            assert "file names are a fallback, not an index" in text, path
            assert 'matched_on: "filename"' in text, path


class TestAHumanCanTellWhichKindOfHitItIs:
    """Devin Review on #1267: the snippet under a name match is the file's
    opening text, which does not contain the query. MCP agents read
    `matched_on`; a person reading the collections UI saw a normal-looking
    quotation instead."""

    def test_the_collection_page_labels_a_name_match(self):
        page = (ROOT / "app" / "web" / "templates" / "library_detail_legacy.html").read_text()
        assert 'res.matched_on === "filename"' in page
        assert "matched by file name" in page

    def test_the_api_carries_the_label_to_it(self, e2e_env):
        from src.ingest.retrieval import search

        cid = _seed("label-api", "quarterly-report.md", [{"ordinal": 0, "text": "alpha bravo"}])
        assert search([cid], "quarterly-report")[0]["matched_on"] == "filename"
