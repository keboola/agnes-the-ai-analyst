"""knowledge packaging (K3, #798) — per-corpus knowledge.duckdb artifacts."""

from unittest.mock import patch

import duckdb
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
        "text": "in EUR only",
        "embedding": [0.1] * 384,
        "section_path": "Billing",
        "page": None,
        "bbox": None,
        "metadata": None,
        "created_at": None,
    },
]
FILES = [{"id": "f1", "filename": "billing.md"}]
CORPORA = [{"id": "col_a", "name": "Handbook"}]


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))


def _patched(chunks=CHUNKS):
    return (
        patch("src.knowledge_packaging._list_chunks", lambda cid: list(chunks)),
        patch("src.knowledge_packaging._list_files", lambda cid: list(FILES)),
        patch("src.knowledge_packaging._list_corpora", lambda: list(CORPORA)),
    )


def test_fingerprint_flips_on_content_change():
    from src.knowledge_packaging import corpus_fingerprint

    p1, p2, p3 = _patched()
    with p1, p2, p3:
        a = corpus_fingerprint("col_a")
        b = corpus_fingerprint("col_a")
    changed = [dict(CHUNKS[0], text="invoices are yearly"), CHUNKS[1]]
    p1, p2, p3 = _patched(changed)
    with p1, p2, p3:
        c = corpus_fingerprint("col_a")
    assert a == b
    assert a != c


def test_build_artifact_writes_chunks_filename_and_meta(tmp_path):
    from src.knowledge_packaging import artifacts_dir, build_artifact

    p1, p2, p3 = _patched()
    with p1, p2, p3:
        info = build_artifact("col_a")
    path = artifacts_dir() / "col_a.duckdb"
    assert path.exists()
    assert info["chunks"] == 2 and info["md5"] and info["size_bytes"] > 0
    con = duckdb.connect(str(path), read_only=True)
    try:
        rows = con.execute("SELECT id, filename, text, embedding FROM chunks ORDER BY ordinal").fetchall()
        meta = dict(con.execute("SELECT key, value FROM artifact_meta").fetchall())
    finally:
        con.close()
    assert rows[0][1] == "billing.md"  # filename denormalized
    assert rows[0][3] is None  # NULL embedding survives
    assert len(rows[1][3]) == 384  # vector survives round-trip
    assert meta["kind"] == "chunks" and meta["corpus_id"] == "col_a"
    assert meta["format_version"] == "1"
    assert not list(artifacts_dir().glob("*.tmp"))  # atomic promotion, no debris


def test_pass_builds_then_skips_unchanged_then_rebuilds():
    from src.knowledge_packaging import run_packaging_pass

    p1, p2, p3 = _patched()
    with p1, p2, p3:
        first = run_packaging_pass()
        second = run_packaging_pass()
    assert first["built"] == ["col_a"] and second["built"] == []
    assert second["skipped"] == ["col_a"]
    changed = [dict(CHUNKS[0], text="edited"), CHUNKS[1]]
    p1, p2, p3 = _patched(changed)
    with p1, p2, p3:
        third = run_packaging_pass()
    assert third["built"] == ["col_a"]


def test_pass_prunes_artifact_for_deleted_corpus():
    from src.knowledge_packaging import artifacts_dir, load_state, run_packaging_pass

    p1, p2, p3 = _patched()
    with p1, p2, p3:
        run_packaging_pass()
    with (
        patch("src.knowledge_packaging._list_chunks", lambda cid: []),
        patch("src.knowledge_packaging._list_files", lambda cid: []),
        patch("src.knowledge_packaging._list_corpora", lambda: []),
    ):
        summary = run_packaging_pass()
    assert summary["pruned"] == ["col_a"]
    assert not (artifacts_dir() / "col_a.duckdb").exists()
    assert "col_a" not in load_state()


def test_empty_corpus_builds_empty_artifact_not_error():
    from src.knowledge_packaging import build_artifact

    with (
        patch("src.knowledge_packaging._list_chunks", lambda cid: []),
        patch("src.knowledge_packaging._list_files", lambda cid: []),
        patch("src.knowledge_packaging._list_corpora", lambda: list(CORPORA)),
    ):
        info = build_artifact("col_a")
    assert info["chunks"] == 0
