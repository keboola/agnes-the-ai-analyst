"""REST endpoint tests for /api/glossary — list, get, search."""

from __future__ import annotations


def test_list_glossary_terms(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["analyst_token"]
    from src.repositories import glossary_repo

    glossary_repo().create(id="a", term="Zeta", definition="z")
    glossary_repo().create(id="b", term="Alpha", definition="a")

    r = c.get("/api/glossary", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    terms = [t["term"] for t in r.json()["terms"]]
    assert terms == ["Alpha", "Zeta"]


def test_list_glossary_terms_requires_auth(seeded_app):
    c = seeded_app["client"]
    r = c.get("/api/glossary")
    assert r.status_code == 401


def test_get_glossary_term_by_id(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["analyst_token"]
    from src.repositories import glossary_repo

    glossary_repo().create(id="kb/m/mrr", term="MRR", definition="Monthly recurring revenue.")

    r = c.get("/api/glossary/kb/m/mrr", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["term"] == "MRR"


def test_get_glossary_term_404(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["analyst_token"]
    r = c.get("/api/glossary/does-not-exist", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 404


def test_search_glossary_terms(seeded_app):
    c = seeded_app["client"]
    token = seeded_app["analyst_token"]
    from src.repositories import glossary_repo

    glossary_repo().create(id="a", term="Churn Rate", definition="Percent of customers lost.")
    glossary_repo().create(id="b", term="Unrelated", definition="Nothing to do with the query.")

    r = c.get("/api/glossary/search", params={"q": "churn"}, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    ids = {t["id"] for t in body["terms"]}
    assert ids == {"a"}
