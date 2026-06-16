"""Tests for the gated vision fallback + runner image routing."""

from __future__ import annotations


def test_media_type_for():
    from src.ingest.vision import media_type_for

    assert media_type_for("PNG") == "image/png"
    assert media_type_for("jpg") == "image/jpeg"
    assert media_type_for(".jpeg") == "image/jpeg"
    assert media_type_for("dwg") is None


def test_vision_unavailable_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    from src.ingest.vision import extract_image_text, vision_available

    assert vision_available() is False
    assert extract_image_text("/nope.png", ext="png") is None


def _img_file(corpus_slug, tmp_path):
    from src.repositories import corpus_files_repo, file_corpora_repo

    cid = file_corpora_repo().create(name=corpus_slug, slug=corpus_slug, description=None, created_by="u")
    img = tmp_path / "p.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n fake")
    fid = corpus_files_repo().add(
        corpus_id=cid,
        filename="p.png",
        sha256="s",
        file_type="png",
        size_bytes=1,
        storage_path=str(img),
    )
    return cid, fid


def test_runner_indexes_image_when_vision_returns_text(e2e_env, tmp_path, monkeypatch):
    import src.ingest.vision as vision

    monkeypatch.setattr(vision, "extract_image_text", lambda path, *, ext: "transcribed text from the scan")
    from src.ingest.runner import ingest_file
    from src.repositories import corpus_chunks_repo, corpus_files_repo

    _cid, fid = _img_file("v-on", tmp_path)
    assert ingest_file(fid) == "indexed"
    detail = corpus_files_repo().get(fid)["processing_detail"]
    assert detail["vision_used"] is True
    assert detail["tier"] == 2
    assert len(corpus_chunks_repo().list_for_file(fid)) >= 1


def test_runner_leaves_image_pending_without_vision(e2e_env, tmp_path, monkeypatch):
    import src.ingest.vision as vision

    monkeypatch.setattr(vision, "extract_image_text", lambda path, *, ext: None)
    from src.ingest.runner import ingest_file
    from src.repositories import corpus_files_repo

    _cid, fid = _img_file("v-off", tmp_path)
    assert ingest_file(fid) == "pending"
    assert corpus_files_repo().get(fid)["processing_status"] == "pending"
