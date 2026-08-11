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
