"""GET /api/knowledge/search — unified knowledge search endpoint (K2, #797)."""

from __future__ import annotations

import io


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_unauthenticated_returns_401(seeded_app):
    c = seeded_app["client"]
    resp = c.get("/api/knowledge/search", params={"q": "anything"})
    assert resp.status_code == 401


def test_admin_gets_typed_results_shape(seeded_app):
    c = seeded_app["client"]
    admin = seeded_app["admin_token"]

    # Seed one collection + document so the chunk source has content.
    col = c.post("/api/collections", json={"name": "KS Col"}, headers=_auth(admin)).json()
    up = c.post(
        f"/api/collections/{col['id']}/files",
        files={"files": ("billing.md", io.BytesIO(b"# Billing\n\nInvoices are generated monthly."), "text/markdown")},
        headers=_auth(admin),
    )
    assert up.status_code == 201, up.text

    resp = c.get("/api/knowledge/search", params={"q": "invoices monthly", "k": 10}, headers=_auth(admin))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["query"] == "invoices monthly"
    assert isinstance(body["results"], list)
    assert body["results"], "expected at least the uploaded chunk to match"
    for hit in body["results"]:
        assert hit["type"] in ("chunk", "knowledge", "table")
        assert "score" in hit
    chunk_hits = [h for h in body["results"] if h["type"] == "chunk"]
    assert any(h["filename"] == "billing.md" for h in chunk_hits)


def test_analyst_without_grants_sees_no_chunks(seeded_app):
    c = seeded_app["client"]
    admin = seeded_app["admin_token"]
    analyst = seeded_app["analyst_token"]

    col = c.post("/api/collections", json={"name": "Private Col"}, headers=_auth(admin)).json()
    up = c.post(
        f"/api/collections/{col['id']}/files",
        files={"files": ("secret.md", io.BytesIO(b"# Secret\n\nThe launch codes are hidden."), "text/markdown")},
        headers=_auth(admin),
    )
    assert up.status_code == 201, up.text

    resp = c.get("/api/knowledge/search", params={"q": "launch codes hidden"}, headers=_auth(analyst))
    assert resp.status_code == 200, resp.text
    chunk_hits = [h for h in resp.json()["results"] if h["type"] == "chunk"]
    assert chunk_hits == []  # fail-closed: no grant on the collection


def test_blank_query_rejected(seeded_app):
    c = seeded_app["client"]
    resp = c.get("/api/knowledge/search", params={"q": ""}, headers=_auth(seeded_app["admin_token"]))
    assert resp.status_code == 422
