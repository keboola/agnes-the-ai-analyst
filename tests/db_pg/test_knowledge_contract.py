"""Cross-engine contract tests for the knowledge repository.

Parametrises over [DuckDB impl, Postgres impl]. The same calls go to
both; the same return shapes must come back. Any divergence is a bug in
whichever side is wrong (DuckDB is the contract authority).

Covers:
- get_votes_by_user  — {item_id: vote} per-user vote map
- count_relations    — filtered COUNT over knowledge_item_relations
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# repo construction helpers — one per backend
# ---------------------------------------------------------------------------


def _make_duckdb_repo(tmp_path):
    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb
    from src.repositories.knowledge import KnowledgeRepository

    conn = _open_duckdb(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    return KnowledgeRepository(conn), conn


def _make_pg_repo(pg_engine, monkeypatch):
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg

    db_pg.dispose()
    db_pg.get_engine()

    from src.repositories.knowledge_pg import KnowledgePgRepository

    return KnowledgePgRepository(db_pg.get_engine()), None


@pytest.fixture(params=["duckdb", "pg"])
def k_repo(request, tmp_path, pg_engine, monkeypatch):
    """Yields a knowledge repo for either DuckDB or PG."""
    backend = request.param
    if backend == "duckdb":
        repo, conn = _make_duckdb_repo(tmp_path)
        yield repo
        if conn is not None:
            conn.close()
    else:
        repo, _ = _make_pg_repo(pg_engine, monkeypatch)
        yield repo


def _create_item(repo, item_id, title="Test item"):
    repo.create(
        id=item_id,
        title=title,
        content="content for " + item_id,
        category="general",
        status="approved",
    )


# ---------------------------------------------------------------------------
# get_votes_by_user contract tests
# ---------------------------------------------------------------------------


def test_get_votes_by_user_empty(k_repo):
    assert k_repo.get_votes_by_user("alice") == {}


def test_get_votes_by_user_upvote(k_repo):
    _create_item(k_repo, "item-1")
    k_repo.vote("item-1", "alice", 1)
    assert k_repo.get_votes_by_user("alice") == {"item-1": 1}


def test_get_votes_by_user_downvote(k_repo):
    _create_item(k_repo, "item-1")
    k_repo.vote("item-1", "alice", -1)
    assert k_repo.get_votes_by_user("alice") == {"item-1": -1}


def test_get_votes_by_user_multiple_items(k_repo):
    _create_item(k_repo, "item-1")
    _create_item(k_repo, "item-2")
    _create_item(k_repo, "item-3")
    k_repo.vote("item-1", "alice", 1)
    k_repo.vote("item-2", "alice", -1)
    # item-3 not voted — must not appear
    k_repo.vote("item-1", "bob", 1)  # other user — must not appear for alice

    result = k_repo.get_votes_by_user("alice")
    assert result == {"item-1": 1, "item-2": -1}


def test_get_votes_by_user_vote_override(k_repo):
    _create_item(k_repo, "item-1")
    k_repo.vote("item-1", "alice", 1)
    k_repo.vote("item-1", "alice", -1)  # override
    assert k_repo.get_votes_by_user("alice") == {"item-1": -1}


def test_get_votes_by_user_after_unvote(k_repo):
    _create_item(k_repo, "item-1")
    k_repo.vote("item-1", "alice", 1)
    k_repo.unvote("item-1", "alice")
    assert k_repo.get_votes_by_user("alice") == {}


# ---------------------------------------------------------------------------
# count_relations contract tests
# ---------------------------------------------------------------------------


def test_count_relations_empty(k_repo):
    assert k_repo.count_relations() == 0


def test_count_relations_total(k_repo):
    _create_item(k_repo, "item-a")
    _create_item(k_repo, "item-b")
    _create_item(k_repo, "item-c")
    k_repo.create_relation("item-a", "item-b", "duplicate")
    k_repo.create_relation("item-a", "item-c", "duplicate")
    assert k_repo.count_relations() == 2


def test_count_relations_filtered_by_type(k_repo):
    _create_item(k_repo, "item-a")
    _create_item(k_repo, "item-b")
    _create_item(k_repo, "item-c")
    k_repo.create_relation("item-a", "item-b", "duplicate")
    k_repo.create_relation("item-a", "item-c", "related")
    assert k_repo.count_relations(relation_type="duplicate") == 1
    assert k_repo.count_relations(relation_type="related") == 1
    assert k_repo.count_relations(relation_type="nonexistent") == 0


def test_count_relations_filtered_by_resolved(k_repo):
    _create_item(k_repo, "item-a")
    _create_item(k_repo, "item-b")
    _create_item(k_repo, "item-c")
    k_repo.create_relation("item-a", "item-b", "duplicate")
    k_repo.create_relation("item-a", "item-c", "duplicate")
    k_repo.resolve_relation("item-a", "item-b", "duplicate", "admin", "merged")

    assert k_repo.count_relations(resolved=False) == 1
    assert k_repo.count_relations(resolved=True) == 1
    assert k_repo.count_relations() == 2


def test_count_relations_type_and_resolved_combined(k_repo):
    _create_item(k_repo, "item-a")
    _create_item(k_repo, "item-b")
    _create_item(k_repo, "item-c")
    k_repo.create_relation("item-a", "item-b", "duplicate")
    k_repo.create_relation("item-a", "item-c", "related")
    k_repo.resolve_relation("item-a", "item-b", "duplicate", "admin", "merged")

    assert k_repo.count_relations(relation_type="duplicate", resolved=True) == 1
    assert k_repo.count_relations(relation_type="duplicate", resolved=False) == 0
    assert k_repo.count_relations(relation_type="related", resolved=False) == 1


# ---------------------------------------------------------------------------
# candidate-status contract (#1017)
#
# The two duplicate/contradiction finders filter on `status`, and the two
# backends disagreed about the set: DuckDB `('approved','pending')`, PG
# `('approved','mandatory','pending')`. `mandatory` is a DEAD status — v49
# split that overload into the orthogonal `is_required` boolean and migrated
# every such row to `is_required=TRUE, status='approved'` (`src/db.py`) — so
# the extra value matched NOTHING and DuckDB was already canonical.
#
# Which means the drift is not behaviourally observable, and the behavioural
# tests below cannot catch it: restoring `'mandatory'` to the PG filter leaves
# every one of them green (verified). They are still worth having — they pin
# the set that IS live, so a change to `('approved',)` or one that swept
# `rejected` back in has to change an assertion — but the literal drift needs
# the static check that follows them.
# ---------------------------------------------------------------------------


def test_both_backends_filter_candidates_on_the_same_status_set():
    """Compare the SQL literals, because behaviour cannot tell them apart.

    A dead status value in one backend's filter is invisible at runtime and
    survives every functional test; the only thing that notices is a reader
    wondering which backend is right. Pinning the literals keeps the answer
    from drifting again — and makes the next person's reintroduction of a
    third value a deliberate act with a test to update.
    """
    duck = (REPO_ROOT / "src" / "repositories" / "knowledge.py").read_text(encoding="utf-8")
    pg = (REPO_ROOT / "src" / "repositories" / "knowledge_pg.py").read_text(encoding="utf-8")

    pattern = re.compile(r"status IN \(([^)]*)\)")

    def status_sets(text):
        found = set()
        for raw in pattern.findall(text):
            # Skip the parameterised forms (`status IN (:st_0, :st_1)`) — those
            # take whatever the caller passes and are not a backend decision.
            if ":" in raw or "?" in raw or "{" in raw:
                continue
            found.add(tuple(sorted(v.strip().strip("'\"") for v in raw.split(","))))
        return found

    duck_sets, pg_sets = status_sets(duck), status_sets(pg)
    assert duck_sets, "no literal status filters found in the DuckDB repo — pattern out of date?"
    assert duck_sets == pg_sets, (
        f"backends filter candidates on different status sets — DuckDB {sorted(duck_sets)} "
        f"vs Postgres {sorted(pg_sets)}. DuckDB is the contract authority (#1017)."
    )
    assert all("mandatory" not in s for s in duck_sets | pg_sets), (
        "`mandatory` is a dead status — v49 migrated those rows to "
        "is_required=TRUE / status='approved'. Filtering on it matches nothing."
    )


@pytest.fixture
def ops_domain(k_repo, tmp_path, pg_engine, monkeypatch):
    """Seed the `ops` memory domain on whichever backend `k_repo` is.

    Needed because the DuckDB repo resolves the slug through `memory_domains`
    and raises on an unknown one, while the PG repo writes the scalar and
    never looks — a divergence in its own right (see #1017), and one this
    fixture papers over so the STATUS contract below can be tested on both.
    """
    if hasattr(k_repo, "conn"):
        from src.repositories.memory_domains import MemoryDomainsRepository

        repo = MemoryDomainsRepository(k_repo.conn)
    else:
        from src.repositories.memory_domains_pg import MemoryDomainsPgRepository

        repo = MemoryDomainsPgRepository(k_repo._engine)
    repo.create(
        name="Ops",
        slug="ops",
        description=None,
        icon=None,
        color=None,
        created_by="system",
    )
    return "ops"


def _create_with_status(repo, item_id, status, entities=None):
    repo.create(
        id=item_id,
        title=f"Item {item_id}",
        content="content for " + item_id,
        category="general",
        status=status,
        domain="ops",
        entities=entities or ["alpha", "beta"],
    )


def test_duplicate_candidates_consider_approved_and_pending(k_repo, ops_domain):
    _create_with_status(k_repo, "seed", "approved")
    _create_with_status(k_repo, "cand-approved", "approved")
    _create_with_status(k_repo, "cand-pending", "pending")

    found = {
        r["id"]
        for r in k_repo.find_duplicate_candidates_by_entities(
            new_item_id="seed", entities=["alpha", "beta"], domain="ops", min_overlap=1
        )
    }
    assert found == {"cand-approved", "cand-pending"}


def test_duplicate_candidates_exclude_other_lifecycle_states(k_repo, ops_domain):
    """`rejected` / `archived` are live status values and must stay out — the
    same query that was over-broad on `mandatory` must not be under-strict
    here."""
    _create_with_status(k_repo, "seed", "approved")
    _create_with_status(k_repo, "cand-rejected", "rejected")
    _create_with_status(k_repo, "cand-archived", "archived")

    found = k_repo.find_duplicate_candidates_by_entities(
        new_item_id="seed", entities=["alpha", "beta"], domain="ops", min_overlap=1
    )
    assert found == []


def test_contradiction_candidates_consider_approved_and_pending(k_repo, ops_domain):
    _create_with_status(k_repo, "seed", "approved")
    _create_with_status(k_repo, "cand-approved", "approved")
    _create_with_status(k_repo, "cand-pending", "pending")
    _create_with_status(k_repo, "cand-rejected", "rejected")

    found = {r["id"] for r in k_repo.find_contradiction_candidates(new_item_id="seed")}
    assert found == {"cand-approved", "cand-pending"}


def test_required_items_are_still_candidates(k_repo, ops_domain):
    """The reason `mandatory` was in the PG filter at all: Required items must
    remain visible to the finders. After v49 they are `status='approved'` with
    `is_required=TRUE`, so the two-value filter already covers them — this is
    the assertion that would fail if someone "restored" the third value by
    changing how Required is stored instead."""
    _create_with_status(k_repo, "seed", "approved")
    k_repo.create(
        id="cand-required",
        title="Required item",
        content="content for required",
        category="general",
        status="approved",
        domain="ops",
        entities=["alpha", "beta"],
        is_required=True,
    )

    found = {
        r["id"]
        for r in k_repo.find_duplicate_candidates_by_entities(
            new_item_id="seed", entities=["alpha", "beta"], domain="ops", min_overlap=1
        )
    }
    assert "cand-required" in found
