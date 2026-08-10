"""An empty collections search must say why, not leave the caller guessing.

Observed on a live instance: a user uploaded one file, the UI showed it as
`indexed` / `Searchable 1 of 1`, and then asked the chat agent what was in
it. The agent ran `collections_search` six times — the question's own words,
the filename, `*`, `md`, `seznam` — got `{"results": [], "retrieval":
"lexical_only"}` every time, tried a `agnes collections cat` that does not
exist, and told the user **"I don't have access to your files or
collections"**. It did have access. That sentence then became the
conversation's permanent title; three more like it were already in the
sidebar, so this is a repeating pattern, not a one-off.

Nothing in an empty result distinguishes "you cannot see any collection"
from "your words are not in the text", so the model picks the scarier
reading. Three properties of the engine make the wrong guesses easy:

  - filenames are NOT indexed (`src/ingest/retrieval.py` ranks chunk text
    only — the filename is attached afterwards, for citations);
  - matching is whole-word, so `test` does not find `Testovaci`;
  - there is no wildcard: `*` and `""` return nothing rather than everything.

The response now carries a `hint` on the empty case naming all three, and
— decisively — how many collections were actually searched, which is what
separates "no access" from "no match".
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _search(seeded_app, token: str, q: str, **params) -> dict:
    r = seeded_app["client"].get("/api/collections/search", params={"q": q, **params}, headers=_auth(token))
    assert r.status_code == 200, r.text
    return r.json()


def _make_collection_with_file(seeded_app, token: str, name: str, body: str) -> dict:
    c = seeded_app["client"].post("/api/collections", json={"name": name}, headers=_auth(token))
    assert c.status_code == 201, c.text
    col = c.json()
    up = seeded_app["client"].post(
        f"/api/collections/{col['id']}/files",
        files={"files": ("note.md", body.encode(), "text/markdown")},
        headers=_auth(token),
    )
    assert up.status_code == 201, up.text
    return col


class TestEmptyResultCarriesAHint:
    def test_no_match_says_it_is_not_an_access_problem(self, seeded_app):
        tok = seeded_app["admin_token"]
        _make_collection_with_file(seeded_app, tok, "Notes", "alpha bravo charlie")

        body = _search(seeded_app, tok, "nosuchwordanywhere")
        assert body["results"] == []
        hint = body.get("hint", "")
        assert hint, "empty result carries no hint"
        # The single most important thing to rule out.
        assert "access" in hint.lower()

    def test_hint_reports_how_many_collections_were_searched(self, seeded_app):
        """The number is what makes 'not an access problem' checkable."""
        tok = seeded_app["admin_token"]
        _make_collection_with_file(seeded_app, tok, "Notes", "alpha bravo charlie")

        body = _search(seeded_app, tok, "nosuchwordanywhere")
        assert body.get("searched_collections", 0) >= 1
        assert str(body["searched_collections"]) in body["hint"]

    def test_hint_names_the_three_engine_surprises(self, seeded_app):
        tok = seeded_app["admin_token"]
        _make_collection_with_file(seeded_app, tok, "Notes", "alpha bravo charlie")

        hint = _search(seeded_app, tok, "nosuchwordanywhere")["hint"].lower()
        assert "filename" in hint, "must say filenames are not indexed"
        assert "whole word" in hint or "whole-word" in hint, "must say matching is whole-word"
        assert "wildcard" in hint or "*" in hint, "must say there is no wildcard"

    def test_a_hit_carries_no_hint(self, seeded_app):
        """The hint is for the dead end only — it must not pad every response."""
        tok = seeded_app["admin_token"]
        _make_collection_with_file(seeded_app, tok, "Notes", "alpha bravo charlie")

        body = _search(seeded_app, tok, "bravo")
        assert body["results"], "precondition: this query matches"
        assert "hint" not in body


class TestNoAccessIsDistinguishable:
    def test_a_caller_with_no_collections_is_told_that_instead(self, seeded_app):
        """Zero accessible collections IS the access case — say so plainly.

        Same empty `results`, different diagnosis: the agent must not tell
        this user to rephrase their query, and must not tell the user above
        that they lack access.
        """
        tok = seeded_app["analyst_token"]
        body = _search(seeded_app, tok, "anything")
        assert body["results"] == []
        if body.get("searched_collections", 0) == 0:
            hint = body["hint"].lower()
            assert "no collection" in hint or "not shared" in hint
            # Must NOT send them chasing better search terms.
            assert "whole word" not in hint and "whole-word" not in hint


class TestRetrievalLabelUnchanged:
    def test_retrieval_mode_still_reported(self, seeded_app):
        tok = seeded_app["admin_token"]
        body = _search(seeded_app, tok, "anything")
        assert body["retrieval"] in ("hybrid", "lexical_only")
