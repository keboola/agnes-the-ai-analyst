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

    def test_a_body_hit_is_labelled_as_one(self):
        import inspect

        from src.ingest import retrieval

        src = inspect.getsource(retrieval.search)
        assert 'matched_on = "body"' in src
        assert 'matched_on = "filename"' in src
        assert '"matched_on": matched_on' in src

    def test_the_combined_search_caps_a_name_only_chunk_bucket(self):
        import inspect

        from src.search import unified

        src = inspect.getsource(unified.unified_search)
        assert 'all(h.get("matched_on") == "filename" for h in chunk_hits)' in src, src[:200]
        i = src.index('matched_on')
        assert "_sem_cap" in src[i : i + 400], "the cap must be the one the semantic buckets use"

    def test_offline_search_has_the_same_fallback(self):
        """`agnes search --local` and the stdio MCP fallback run this path; the
        module promises "the exact same ranking behavior" as the server."""
        import inspect

        from src.search import local

        src = inspect.getsource(local.local_search)
        assert "_rank_by_filename" in src
        assert 'matched_on = "filename"' in src
        assert 'confidence = "low"' in src


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
