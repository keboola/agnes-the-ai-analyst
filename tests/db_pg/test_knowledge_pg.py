"""Postgres-side tests for the knowledge cluster:
knowledge_items, knowledge_votes, knowledge_item_user_dismissed,
knowledge_contradictions, verification_evidence, knowledge_item_relations.
"""
from __future__ import annotations

from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def k_engine(pg_engine, monkeypatch):
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg
    db_pg.dispose()
    return db_pg.get_engine()


# ---------------------------------------------------------------------------
# items: create / get / update
# ---------------------------------------------------------------------------

def test_knowledge_create_and_get(k_engine):
    from src.repositories.knowledge_pg import KnowledgePgRepository

    repo = KnowledgePgRepository(k_engine)
    repo.create(
        id="k1", title="Pricing Policy", content="USD only.",
        category="finance", source_user="alice",
        tags=["billing", "tax"], status="approved",
        domain="finance", entities=["pricing", "USD"],
    )
    item = repo.get_by_id("k1")
    assert item["title"] == "Pricing Policy"
    assert item["status"] == "approved"
    assert item["tags"] == ["billing", "tax"]
    assert item["entities"] == ["pricing", "USD"]


def test_knowledge_get_by_ids(k_engine):
    from src.repositories.knowledge_pg import KnowledgePgRepository

    repo = KnowledgePgRepository(k_engine)
    repo.create(id="k1", title="a", content="x", category="c")
    repo.create(id="k2", title="b", content="y", category="c")
    repo.create(id="k3", title="c", content="z", category="c")
    out = repo.get_by_ids(["k1", "k3"])
    assert set(out) == {"k1", "k3"}


def test_knowledge_update_status(k_engine):
    from src.repositories.knowledge_pg import KnowledgePgRepository

    repo = KnowledgePgRepository(k_engine)
    repo.create(id="k1", title="t", content="c", category="c")
    repo.update_status("k1", "approved")
    assert repo.get_by_id("k1")["status"] == "approved"


def test_knowledge_update_partial_with_tags(k_engine):
    from src.repositories.knowledge_pg import KnowledgePgRepository

    repo = KnowledgePgRepository(k_engine)
    repo.create(id="k1", title="t", content="c", category="cat", tags=["a"])
    repo.update("k1", tags=["a", "b"], status="approved")
    item = repo.get_by_id("k1")
    assert item["tags"] == ["a", "b"]
    assert item["status"] == "approved"


# ---------------------------------------------------------------------------
# list_items + search + count_items
# ---------------------------------------------------------------------------

def test_knowledge_list_items_filters(k_engine):
    from src.repositories.knowledge_pg import KnowledgePgRepository

    repo = KnowledgePgRepository(k_engine)
    repo.create(id="k1", title="a", content="c", category="x",
                domain="finance", status="approved")
    repo.create(id="k2", title="b", content="c", category="y",
                domain="finance", status="pending")
    repo.create(id="k3", title="c", content="c", category="x",
                domain="ops", status="approved")

    rows = repo.list_items(statuses=["approved"], category="x")
    assert {r["id"] for r in rows} == {"k1", "k3"}

    rows = repo.list_items(domain="finance")
    assert {r["id"] for r in rows} == {"k1", "k2"}


def test_knowledge_search_finds_by_content(k_engine):
    from src.repositories.knowledge_pg import KnowledgePgRepository

    repo = KnowledgePgRepository(k_engine)
    repo.create(id="k1", title="Revenue policy",
                content="We use USD for all invoices.",
                category="finance", status="approved")
    repo.create(id="k2", title="Latency targets",
                content="P95 under 300ms.", category="ops",
                status="approved")

    rows = repo.search("invoices")
    ids = {r["id"] for r in rows}
    assert "k1" in ids
    assert "k2" not in ids


def test_knowledge_count_items_matches_list(k_engine):
    from src.repositories.knowledge_pg import KnowledgePgRepository

    repo = KnowledgePgRepository(k_engine)
    for i in range(5):
        repo.create(id=f"k{i}", title=f"t{i}", content="c",
                    category="x", status="approved")
    assert repo.count_items(statuses=["approved"]) == 5
    assert len(repo.list_items(statuses=["approved"], limit=100)) == 5


# ---------------------------------------------------------------------------
# votes
# ---------------------------------------------------------------------------

def test_knowledge_vote_aggregates(k_engine):
    from src.repositories.knowledge_pg import KnowledgePgRepository

    repo = KnowledgePgRepository(k_engine)
    repo.create(id="k1", title="t", content="c", category="c")
    repo.vote("k1", "u1", 1)
    repo.vote("k1", "u2", 1)
    repo.vote("k1", "u3", -1)
    v = repo.get_votes("k1")
    assert v == {"upvotes": 2, "downvotes": 1}

    # Re-vote overwrites
    repo.vote("k1", "u3", 1)
    v = repo.get_votes("k1")
    assert v == {"upvotes": 3, "downvotes": 0}

    repo.unvote("k1", "u3")
    v = repo.get_votes("k1")
    assert v == {"upvotes": 2, "downvotes": 0}


# ---------------------------------------------------------------------------
# dismissals
# ---------------------------------------------------------------------------

def test_knowledge_dismiss_idempotent(k_engine):
    from src.repositories.knowledge_pg import KnowledgePgRepository

    repo = KnowledgePgRepository(k_engine)
    repo.create(id="k1", title="t", content="c", category="c")
    repo.dismiss("u1", "k1")
    repo.dismiss("u1", "k1")  # idempotent
    assert repo.is_dismissed("u1", "k1")
    assert repo.list_dismissed_ids("u1") == ["k1"]
    repo.undismiss("u1", "k1")
    assert not repo.is_dismissed("u1", "k1")
    # Un-dismiss again is also a no-op (no error)
    repo.undismiss("u1", "k1")


def test_knowledge_list_items_hides_dismissed_but_not_mandatory(k_engine):
    from src.repositories.knowledge_pg import KnowledgePgRepository

    repo = KnowledgePgRepository(k_engine)
    repo.create(id="k1", title="dismissable", content="c", category="c",
                status="approved")
    repo.create(id="k_mand", title="mandatory", content="c", category="c",
                status="mandatory")
    repo.dismiss("u1", "k1")
    repo.dismiss("u1", "k_mand")
    rows = repo.list_items(hide_dismissed=True, dismissed_by_user="u1")
    ids = {r["id"] for r in rows}
    assert "k1" not in ids
    assert "k_mand" in ids  # mandatory never hidden


# ---------------------------------------------------------------------------
# contradictions
# ---------------------------------------------------------------------------

def test_knowledge_create_and_resolve_contradiction(k_engine):
    from src.repositories.knowledge_pg import KnowledgePgRepository

    repo = KnowledgePgRepository(k_engine)
    repo.create(id="ka", title="a", content="c", category="c")
    repo.create(id="kb", title="b", content="c", category="c")
    cid = repo.create_contradiction(
        item_a_id="ka", item_b_id="kb",
        explanation="conflict",
        suggested_resolution={"merged_content": "..."},
    )
    row = repo.get_contradiction(cid)
    assert row["explanation"] == "conflict"
    # dict round-trip
    assert row["suggested_resolution"]["merged_content"] == "..."

    repo.resolve_contradiction(cid, resolved_by="admin", resolution="merge_a")
    row = repo.get_contradiction(cid)
    assert row["resolved"] is True
    assert row["resolution"] == "merge_a"


# ---------------------------------------------------------------------------
# evidence
# ---------------------------------------------------------------------------

def test_knowledge_evidence_create_and_list(k_engine):
    from src.repositories.knowledge_pg import KnowledgePgRepository

    repo = KnowledgePgRepository(k_engine)
    repo.create(id="k1", title="t", content="c", category="c")
    eid = repo.create_evidence("k1", source_user="bob",
                                detection_type="quote",
                                user_quote="they said X")
    assert eid.startswith("ev_")
    rows = repo.list_evidence("k1")
    assert len(rows) == 1
    assert rows[0]["id"] == eid
    assert rows[0]["user_quote"] == "they said X"


# ---------------------------------------------------------------------------
# relations
# ---------------------------------------------------------------------------

def test_knowledge_relation_canonicalizes_pair(k_engine):
    from src.repositories.knowledge_pg import KnowledgePgRepository

    repo = KnowledgePgRepository(k_engine)
    repo.create(id="b", title="b", content="c", category="c")
    repo.create(id="a", title="a", content="c", category="c")
    repo.create_relation("b", "a", "duplicate")
    # Reversed order — same row (canonical PK)
    repo.create_relation("a", "b", "duplicate")
    rels = repo.list_relations(relation_type="duplicate")
    assert len(rels) == 1
    assert rels[0]["item_a_id"] == "a"
    assert rels[0]["item_b_id"] == "b"


def test_knowledge_relation_resolve(k_engine):
    from src.repositories.knowledge_pg import KnowledgePgRepository

    repo = KnowledgePgRepository(k_engine)
    repo.create(id="a", title="a", content="c", category="c")
    repo.create(id="b", title="b", content="c", category="c")
    repo.create_relation("a", "b", "dup", score=0.7)
    rc = repo.resolve_relation("a", "b", "dup", resolved_by="admin",
                                resolution="merge")
    assert rc == 1
    rel = repo.get_relation("a", "b", "dup")
    assert rel["resolved"] is True


# ---------------------------------------------------------------------------
# duplicate-candidate finder + aggregations
# ---------------------------------------------------------------------------

def test_knowledge_find_duplicate_candidates(k_engine):
    from src.repositories.knowledge_pg import KnowledgePgRepository

    repo = KnowledgePgRepository(k_engine)
    repo.create(id="new", title="t", content="c", category="c",
                domain="ops", entities=["alpha", "beta", "gamma"],
                status="approved")
    repo.create(id="match", title="t", content="c", category="c",
                domain="ops", entities=["alpha", "beta", "delta"],
                status="approved")
    repo.create(id="diff", title="t", content="c", category="c",
                domain="ops", entities=["x", "y", "z"],
                status="approved")
    repo.create(id="wrong_domain", title="t", content="c", category="c",
                domain="finance", entities=["alpha", "beta", "gamma"],
                status="approved")
    candidates = repo.find_duplicate_candidates_by_entities(
        "new", ["alpha", "beta", "gamma"], "ops", min_overlap=2,
    )
    ids = {c["id"] for c in candidates}
    assert "match" in ids
    assert "diff" not in ids
    assert "wrong_domain" not in ids
    assert "new" not in ids


def test_knowledge_count_by_tag_and_audience(k_engine):
    from src.repositories.knowledge_pg import KnowledgePgRepository

    repo = KnowledgePgRepository(k_engine)
    repo.create(id="k1", title="t", content="c", category="c",
                tags=["billing", "tax"], status="approved")
    repo.create(id="k2", title="t", content="c", category="c",
                tags=["billing"], status="approved")
    repo.create(id="k3", title="t", content="c", category="c",
                tags=[], status="approved")
    by_tag = repo.count_by_tag()
    assert by_tag.get("billing") == 2
    assert by_tag.get("tax") == 1


def test_knowledge_bulk_update(k_engine):
    from src.repositories.knowledge_pg import KnowledgePgRepository

    repo = KnowledgePgRepository(k_engine)
    repo.create(id="k1", title="t", content="c", category="c",
                tags=["x"], status="pending")
    repo.create(id="k2", title="t", content="c", category="c",
                tags=["x"], status="pending")
    res = repo.bulk_update(
        ["k1", "k2", "nope"],
        {"status": "approved", "tags_add": ["y"]},
    )
    assert res["k1"] == "updated"
    assert res["k2"] == "updated"
    assert res["nope"] == "not_found"
    item = repo.get_by_id("k1")
    assert item["status"] == "approved"
    assert sorted(item["tags"]) == ["x", "y"]
