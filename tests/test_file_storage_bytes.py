"""store_corpus_bytes — sync content-addressed writes for bundle members (K1)."""

import pytest
from fastapi import HTTPException


def test_store_bytes_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.file_storage import store_corpus_bytes

    s = store_corpus_bytes("cor_1", "page.html", b"<html>hi</html>")
    assert s.size_bytes == 15
    assert s.ext == ".html"
    assert s.storage_path.endswith(f"{s.sha256}.html")
    with open(s.storage_path, "rb") as fh:
        assert fh.read() == b"<html>hi</html>"
    # idempotent: same bytes → same path regardless of filename stem
    s2 = store_corpus_bytes("cor_1", "other-name.html", b"<html>hi</html>")
    assert s2.storage_path == s.storage_path


def test_store_bytes_path_traversal_neutralised(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.file_storage import store_corpus_bytes

    s = store_corpus_bytes("cor_1", "../../etc/passwd.txt", b"data")
    assert str(tmp_path / "file_corpora" / "cor_1") in s.storage_path


def test_store_bytes_empty_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.file_storage import store_corpus_bytes

    with pytest.raises(HTTPException) as exc:
        store_corpus_bytes("cor_1", "empty.txt", b"")
    assert exc.value.status_code == 400


def test_store_bytes_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import src.file_storage as fs

    monkeypatch.setattr(fs, "MAX_UPLOAD_BYTES", 4)
    with pytest.raises(HTTPException) as exc:
        fs.store_corpus_bytes("cor_1", "big.txt", b"12345")
    assert exc.value.status_code == 413
