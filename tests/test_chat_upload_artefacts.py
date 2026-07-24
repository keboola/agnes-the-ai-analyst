"""Chat "+" uploads bridge to Artefacts.

A document/image dropped in chat is also persisted as a private single-file
artefact (so it shows in Artefacts and is searchable), while data files stay
workspace-only (their job is to become a queryable table). See
``app/corpus_ingest.py`` + ``app/api/chat_uploads.py``.
"""

from __future__ import annotations

import io


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _chat_upload(seeded_app, filename: str, content: bytes, ctype: str, kind: str):
    return seeded_app["client"].post(
        "/api/chat/uploads",
        files={"file": (filename, io.BytesIO(content), ctype)},
        data={"kind": kind},
        headers=_auth(seeded_app["admin_token"]),
    )


def test_chat_document_persists_as_artefact(seeded_app):
    r = _chat_upload(seeded_app, "brief.txt", b"hello world from chat", "text/plain", "document")
    assert r.status_code == 200, r.text
    body = r.json()
    slug = body.get("artefact_slug")
    assert slug, f"expected artefact_slug in response, got {body}"
    assert "Saved to your Artefacts" in body["hint"]

    # It appears in the caller's own collections…
    items = seeded_app["client"].get("/api/collections", headers=_auth(seeded_app["admin_token"])).json()["items"]
    assert any(i["slug"] == slug for i in items), "artefact not listed in /api/collections"

    # …and on the Artefacts page, presented AS the file (filename shown).
    art = seeded_app["client"].get("/artefacts", headers=_auth(seeded_app["admin_token"])).text
    assert "brief.txt" in art


def test_chat_image_persists_as_artefact(seeded_app):
    r = _chat_upload(seeded_app, "diagram.png", b"\x89PNG\r\n\x1a\n" + b"0" * 40, "image/png", "image")
    assert r.status_code == 200, r.text
    assert r.json().get("artefact_slug"), "image should also persist as an artefact"


def test_chat_data_file_stays_workspace_only(seeded_app):
    r = _chat_upload(seeded_app, "nums.csv", b"a,b\n1,2\n", "text/csv", "data")
    assert r.status_code == 200, r.text
    # Data files are for querying, not searchable prose — no artefact created.
    assert r.json().get("artefact_slug") is None
