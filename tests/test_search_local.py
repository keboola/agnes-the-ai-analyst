"""agnes search --local — hybrid ranking over pulled knowledge artifacts (K3)."""

from unittest.mock import patch

import pytest

CHUNKS = [
    {
        "id": "ck1",
        "corpus_id": "col_a",
        "file_id": "f1",
        "ordinal": 0,
        "text": "invoices are monthly",
        "embedding": None,
        "section_path": None,
        "page": None,
        "bbox": None,
        "metadata": None,
        "created_at": None,
    },
    {
        "id": "ck2",
        "corpus_id": "col_a",
        "file_id": "f1",
        "ordinal": 1,
        "text": "vacation policy is generous",
        "embedding": None,
        "section_path": None,
        "page": None,
        "bbox": None,
        "metadata": None,
        "created_at": None,
    },
]


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Workspace with one artifact built by the REAL packaging builder —
    schema drift between builder and reader fails here first."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "srv"))
    from src.knowledge_packaging import artifacts_dir, build_artifact

    with (
        patch("src.knowledge_packaging._list_chunks", lambda cid: list(CHUNKS)),
        patch("src.knowledge_packaging._list_files", lambda cid: [{"id": "f1", "filename": "handbook.md"}]),
        patch("src.knowledge_packaging._list_corpora", lambda: [{"id": "col_a", "name": "Handbook"}]),
    ):
        build_artifact("col_a")
    ws = tmp_path / "ws"
    kdir = ws / "user" / "knowledge"
    kdir.mkdir(parents=True)
    (artifacts_dir() / "col_a.duckdb").rename(kdir / "col_a.duckdb")
    return ws


def test_local_search_ranks_and_cites(workspace):
    from src.search.local import local_search

    hits = local_search("monthly invoices", workspace=workspace, k=5)
    assert hits and hits[0]["chunk_id"] == "ck1"
    assert hits[0]["type"] == "chunk"
    assert hits[0]["filename"] == "handbook.md"
    assert hits[0]["confidence"] in {"high", "medium", "low"}


def test_no_artifacts_returns_empty(tmp_path):
    from src.search.local import local_search

    assert local_search("anything", workspace=tmp_path) == []


def test_blank_query_returns_empty(workspace):
    from src.search.local import local_search

    assert local_search("   ", workspace=workspace) == []


def test_rank_parity_with_server_engine(workspace):
    """Local ranking == server ranking over the identical candidate set."""
    from src.ingest.retrieval import rank_chunks
    from src.search.local import local_search

    server_top, _conf = rank_chunks(list(CHUNKS), "monthly invoices", k=5)
    local_hits = local_search("monthly invoices", workspace=workspace, k=5)
    assert [c["id"] for _s, c in server_top] == [h["chunk_id"] for h in local_hits]


def test_filename_fallback_matches_the_server(workspace):
    """Offline search must find a file by name, like the server does.

    `local_search` calls `rank_chunks` directly, so the filename fallback
    added to `search()` did not reach it — `agnes search --local` and the
    stdio MCP fallback kept answering `[]` for a query the server now
    resolves. The artifact schema already denormalizes `filename` onto the
    chunk rows, so the two surfaces can behave identically. Devin Review
    on #1267.
    """
    from src.search.local import local_search

    hits = local_search("handbook", workspace=workspace, k=5)
    assert hits, "a file cannot be found by its name offline"
    assert hits[0]["filename"] == "handbook.md"
    assert hits[0]["match"] == "filename"


def test_content_hits_offline_are_labelled_content(workspace):
    from src.search.local import local_search

    hits = local_search("monthly invoices", workspace=workspace, k=5)
    assert hits and hits[0]["match"] == "content"


def test_offline_extension_alone_still_matches_nothing(workspace):
    from src.search.local import local_search

    assert local_search("md", workspace=workspace, k=5) == []
