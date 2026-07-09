"""Web UI: /library/{slug} file cards must badge needs_review/rejected files
and surface the failure reason (not render bare, unstyled status text)."""

from __future__ import annotations

from pathlib import Path


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _new_corpus(slug: str) -> str:
    from src.repositories import file_corpora_repo

    return file_corpora_repo().create(name=slug, slug=slug, description=None, created_by="admin1")


def _add_file(corpus_id: str, filename: str, file_type: str, path: str) -> str:
    from src.repositories import corpus_files_repo

    return corpus_files_repo().add(
        corpus_id=corpus_id,
        filename=filename,
        sha256="sha_" + filename,
        file_type=file_type,
        size_bytes=Path(path).stat().st_size if Path(path).exists() else 0,
        storage_path=path,
    )


def test_library_detail_shows_needs_review_reason(seeded_app, tmp_path):
    """File card must badge needs_review and surface the reason text."""
    from src.repositories import corpus_files_repo

    doc = tmp_path / "empty.csv"
    doc.write_text("col_a,col_b\n")

    corpus_id = _new_corpus("needs-review-ui")
    file_id = _add_file(corpus_id, "empty.csv", "csv", str(doc))
    corpus_files_repo().set_status(
        file_id,
        status="needs_review",
        detail={"reason": "extraction produced empty table"},
    )

    r = seeded_app["client"].get("/library/needs-review-ui", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    assert "needs_review" in r.text
    assert "extraction produced empty table" in r.text


def test_library_detail_shows_rejected_reason(seeded_app, tmp_path):
    """Rejected files also badge + surface their reason."""
    from src.repositories import corpus_files_repo

    doc = tmp_path / "bad.docx"
    doc.write_text("not really a docx")

    corpus_id = _new_corpus("rejected-ui")
    file_id = _add_file(corpus_id, "bad.docx", "docx", str(doc))
    corpus_files_repo().set_status(
        file_id,
        status="rejected",
        detail={"reason": "unsupported or corrupt file"},
    )

    r = seeded_app["client"].get("/library/rejected-ui", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    assert "rejected" in r.text
    assert "unsupported or corrupt file" in r.text


def test_library_detail_admin_sees_reingest_button(seeded_app, tmp_path):
    """Admin viewing a needs_review file must see a re-ingest button."""
    from src.repositories import corpus_files_repo

    doc = tmp_path / "review.csv"
    doc.write_text("col_a,col_b\n")

    corpus_id = _new_corpus("reingest-ui")
    file_id = _add_file(corpus_id, "review.csv", "csv", str(doc))
    corpus_files_repo().set_status(
        file_id,
        status="needs_review",
        detail={"reason": "extraction produced empty table"},
    )

    r = seeded_app["client"].get("/library/reingest-ui", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    assert "data-reingest" in r.text


def test_library_detail_indexed_file_has_no_reason(seeded_app, tmp_path):
    """A clean, indexed file must not render a reason block."""
    from src.repositories import corpus_files_repo

    doc = tmp_path / "good.csv"
    doc.write_text("col_a,col_b\n1,2\n")

    corpus_id = _new_corpus("indexed-ui")
    file_id = _add_file(corpus_id, "good.csv", "csv", str(doc))
    corpus_files_repo().set_status(file_id, status="indexed", detail=None)

    r = seeded_app["client"].get("/library/indexed-ui", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    assert "indexed" in r.text
    assert '<div class="file__reason">' not in r.text
