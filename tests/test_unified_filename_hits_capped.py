"""A filename match must not crowd out results that matched real words.

`unified_search` min-max normalizes EACH bucket on its own, so whatever
tops a bucket scores 1.0 regardless of how weak it actually was — which is
why metrics and glossary already carry a `_sem_cap`. Filename hits are the
weakest signal of all: the query never appeared in any document body.

Worse, the sort's tie-break is `h["type"]` and `"chunk"` sorts first
alphabetically, so at equal normalized score a filename hit beats a
glossary definition, a knowledge note and a table card every time. A query
like "revenue" could return one file named `revenue.md` and nothing else,
hiding the glossary entry that defines the term.

Devin Review on #1267.
"""

from __future__ import annotations

from unittest.mock import patch

import src.search.unified as unified


def _chunk(i: int, match: str) -> dict:
    return {
        "chunk_id": f"c{i}",
        "corpus_id": "col_1",
        "file_id": f"f{i}",
        "filename": f"revenue-{i}.md",
        "ordinal": 0,
        "text": "alpha bravo",
        "score": 1.0,
        "confidence": "low",
        "match": match,
    }


def _glossary_rows(n: int) -> list:
    return [{"id": f"g{i}", "term": f"revenue {i}", "definition": "money in"} for i in range(n)]


class TestFilenameHitsAreCapped:
    def test_filename_only_chunks_do_not_fill_every_slot(self):
        """The bucket is capped like the other weak-signal buckets."""
        chunks = [_chunk(i, "filename") for i in range(10)]
        with patch.object(unified, "_chunk_search", return_value=chunks):
            with patch.object(unified, "_glossary_search", return_value=_glossary_rows(5)):
                out = unified.unified_search(
                    "revenue",
                    corpus_ids=["col_1"],
                    user_groups=None,
                    granted_domains=None,
                    tables=[],
                    metrics=[],
                    k=10,
                )

        kinds = [h["type"] for h in out]
        assert kinds.count("chunk") <= 2, f"filename hits took {kinds.count('chunk')} of {len(out)} slots: {kinds}"
        assert "glossary" in kinds, "a real term definition was crowded out by filename matches"

    def test_content_chunks_keep_their_full_share(self):
        """Only the weak kind is capped — a real body match still wins."""
        chunks = [_chunk(i, "content") for i in range(10)]
        with patch.object(unified, "_chunk_search", return_value=chunks):
            with patch.object(unified, "_glossary_search", return_value=_glossary_rows(5)):
                out = unified.unified_search(
                    "revenue",
                    corpus_ids=["col_1"],
                    user_groups=None,
                    granted_domains=None,
                    tables=[],
                    metrics=[],
                    k=10,
                )

        kinds = [h["type"] for h in out]
        assert kinds.count("chunk") > 2, f"content hits were capped like filename hits: {kinds}"

    def test_unlabelled_chunks_are_treated_as_content(self):
        """Back-compat: a caller that does not set `match` is not penalised."""
        chunks = [{k: v for k, v in _chunk(i, "content").items() if k != "match"} for i in range(10)]
        with patch.object(unified, "_chunk_search", return_value=chunks):
            with patch.object(unified, "_glossary_search", return_value=[]):
                out = unified.unified_search(
                    "revenue",
                    corpus_ids=["col_1"],
                    user_groups=None,
                    granted_domains=None,
                    tables=[],
                    metrics=[],
                    k=10,
                )

        assert [h["type"] for h in out].count("chunk") > 2


class TestSearchLabelsItsHits:
    def test_filename_fallback_rows_are_labelled(self, e2e_env):
        from src.ingest.retrieval import search
        from src.repositories import corpus_chunks_repo, corpus_files_repo, file_corpora_repo

        cid = file_corpora_repo().create(name="lbl", slug="lbl", description=None, created_by="u")
        fid = corpus_files_repo().add(
            corpus_id=cid,
            filename="quarterly-report.md",
            sha256="s",
            file_type="md",
            size_bytes=1,
            storage_path="/x",
        )
        corpus_chunks_repo().add_many([{"corpus_id": cid, "file_id": fid, "ordinal": 0, "text": "alpha"}])

        by_name = search([cid], "quarterly-report")
        assert by_name and by_name[0]["match"] == "filename"

        by_text = search([cid], "alpha")
        assert by_text and by_text[0]["match"] == "content"
