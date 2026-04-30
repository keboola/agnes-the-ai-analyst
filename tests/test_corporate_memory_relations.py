"""Tests for knowledge_item_relations + duplicate-candidate detector hook.

Issue #62 — schema v17, repository CRUD, canonical pair ordering, detector
integration, threshold edge cases.
"""

import json
from unittest.mock import MagicMock

import pytest

from src.repositories.knowledge import KnowledgeRepository
from services.verification_detector.duplicates import (
    MIN_ENTITY_OVERLAP,
    RELATION_TYPE,
    _record_duplicate_candidates,
)


def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import src.db as db_module
    db_module._system_db_conn = None
    db_module._system_db_path = None
    return db_module.get_system_db()


# ---------------------------------------------------------------------------
# Schema v17
# ---------------------------------------------------------------------------


class TestSchemaV17:
    def test_fresh_install_has_relations_table(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        tables = {
            row[0] for row in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
            ).fetchall()
        }
        assert "knowledge_item_relations" in tables
        conn.close()

    def test_schema_version_at_target(self, tmp_path, monkeypatch):
        """Fresh install lands at the current SCHEMA_VERSION target. Not
        pinned to v17 — the relations table was introduced there but the
        schema has moved on (v18 dropped stranded google memberships)."""
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.db import SCHEMA_VERSION, get_schema_version
        assert get_schema_version(conn) == SCHEMA_VERSION
        assert SCHEMA_VERSION >= 17, "knowledge_item_relations was added at v17"
        conn.close()

    def test_relations_table_columns(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        cols = {
            row[0] for row in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'knowledge_item_relations'"
            ).fetchall()
        }
        expected = {
            "item_a_id", "item_b_id", "relation_type", "score",
            "resolved", "resolved_by", "resolved_at", "resolution",
            "created_at",
        }
        assert expected.issubset(cols), f"missing: {expected - cols}"
        conn.close()


# ---------------------------------------------------------------------------
# Repository methods
# ---------------------------------------------------------------------------


class TestRelationsCRUD:
    def test_create_canonicalizes_pair(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        repo = KnowledgeRepository(conn)
        # Create with reversed order — should still resolve to same row.
        repo.create_relation("kv_b", "kv_a", "likely_duplicate", score=0.5)
        repo.create_relation("kv_a", "kv_b", "likely_duplicate", score=0.99)
        rows = conn.execute("SELECT * FROM knowledge_item_relations").fetchall()
        assert len(rows) == 1
        # First INSERT wins under ON CONFLICT DO NOTHING — so score stays 0.5.
        rel = repo.get_relation("kv_a", "kv_b", "likely_duplicate")
        assert rel is not None
        assert rel["item_a_id"] == "kv_a"  # min
        assert rel["item_b_id"] == "kv_b"  # max
        conn.close()

    def test_create_rejects_self_relation(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        repo = KnowledgeRepository(conn)
        with pytest.raises(ValueError):
            repo.create_relation("kv_x", "kv_x", "likely_duplicate")
        conn.close()

    def test_list_relations_filters_resolved(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        repo = KnowledgeRepository(conn)
        repo.create_relation("kv_a", "kv_b", "likely_duplicate")
        repo.create_relation("kv_c", "kv_d", "likely_duplicate")
        repo.resolve_relation("kv_a", "kv_b", "likely_duplicate", "admin@x", "duplicate")
        unresolved = repo.list_relations(relation_type="likely_duplicate", resolved=False)
        resolved = repo.list_relations(relation_type="likely_duplicate", resolved=True)
        assert len(unresolved) == 1
        assert len(resolved) == 1
        conn.close()

    def test_resolve_relation_returns_zero_when_missing(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        repo = KnowledgeRepository(conn)
        n = repo.resolve_relation("kv_x", "kv_y", "likely_duplicate", "u", "duplicate")
        assert n == 0
        conn.close()


class TestFindDuplicateCandidatesByEntities:
    def _seed(self, repo, item_id, entities, domain="finance", status="approved"):
        repo.create(
            id=item_id,
            title=f"Item {item_id}",
            content=f"content of {item_id}",
            category="business_logic",
            entities=entities,
            domain=domain,
            status=status,
        )

    def test_no_match_below_threshold(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        repo = KnowledgeRepository(conn)
        self._seed(repo, "kv_old", ["A", "B", "C"])
        # 1 shared entity → below MIN_ENTITY_OVERLAP=2 → no match
        cands = repo.find_duplicate_candidates_by_entities(
            new_item_id="kv_new", entities=["A", "X", "Y"],
            domain="finance", min_overlap=2,
        )
        assert cands == []
        conn.close()

    def test_match_at_threshold(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        repo = KnowledgeRepository(conn)
        self._seed(repo, "kv_old", ["A", "B", "C"])
        # 2 shared entities (A, B); union = {A,B,C,X} → Jaccard = 2/4 = 0.5
        cands = repo.find_duplicate_candidates_by_entities(
            new_item_id="kv_new", entities=["A", "B", "X"],
            domain="finance", min_overlap=2,
        )
        assert len(cands) == 1
        assert cands[0]["overlap_count"] == 2
        assert cands[0]["jaccard"] == pytest.approx(0.5)
        conn.close()

    def test_excludes_personal_items(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        repo = KnowledgeRepository(conn)
        repo.create(
            id="kv_personal", title="t", content="c", category="x",
            entities=["A", "B", "C"], domain="finance",
            status="approved", is_personal=True,
        )
        cands = repo.find_duplicate_candidates_by_entities(
            new_item_id="kv_new", entities=["A", "B", "X"],
            domain="finance", min_overlap=2,
        )
        assert cands == []
        conn.close()

    def test_excludes_self(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        repo = KnowledgeRepository(conn)
        self._seed(repo, "kv_self", ["A", "B", "C"])
        cands = repo.find_duplicate_candidates_by_entities(
            new_item_id="kv_self", entities=["A", "B"],
            domain="finance", min_overlap=2,
        )
        assert cands == []
        conn.close()

    def test_null_domain_returns_empty(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        repo = KnowledgeRepository(conn)
        cands = repo.find_duplicate_candidates_by_entities(
            new_item_id="kv_new", entities=["A", "B"],
            domain=None, min_overlap=2,
        )
        assert cands == []
        conn.close()

    def test_different_domain_skipped(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        repo = KnowledgeRepository(conn)
        self._seed(repo, "kv_old", ["A", "B", "C"], domain="product")
        cands = repo.find_duplicate_candidates_by_entities(
            new_item_id="kv_new", entities=["A", "B"],
            domain="finance", min_overlap=2,
        )
        assert cands == []
        conn.close()


# ---------------------------------------------------------------------------
# Detector hook
# ---------------------------------------------------------------------------


class TestDetectorHook:
    def test_records_relation_when_overlap_meets_threshold(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        repo = KnowledgeRepository(conn)
        repo.create(
            id="kv_old",
            title="Existing",
            content="content",
            category="business_logic",
            entities=["NPS", "rolling 90-day"],
            domain="product",
            status="approved",
        )
        new_item = {
            "id": "kv_new",
            "entities": ["NPS", "rolling 90-day", "MAU"],
            "domain": "product",
        }
        # Need to also create the new item in DB so repo.create_relation works
        repo.create(
            id="kv_new",
            title="New",
            content="c",
            category="business_logic",
            entities=new_item["entities"],
            domain="product",
            status="pending",
        )
        n = _record_duplicate_candidates(repo, repo.get_by_id("kv_new"))
        assert n == 1
        rels = repo.list_relations(relation_type=RELATION_TYPE)
        assert len(rels) == 1
        assert rels[0]["score"] is not None
        conn.close()

    def test_no_relation_when_only_one_entity_shared(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        repo = KnowledgeRepository(conn)
        repo.create(
            id="kv_old", title="t", content="c", category="x",
            entities=["A"], domain="product", status="approved",
        )
        repo.create(
            id="kv_new", title="t", content="c", category="x",
            entities=["A", "B"], domain="product", status="pending",
        )
        n = _record_duplicate_candidates(repo, repo.get_by_id("kv_new"))
        assert n == 0
        assert repo.list_relations(relation_type=RELATION_TYPE) == []
        conn.close()

    def test_no_relation_when_domain_missing(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        repo = KnowledgeRepository(conn)
        repo.create(
            id="kv_new", title="t", content="c", category="x",
            entities=["A", "B", "C"], status="pending",
        )
        n = _record_duplicate_candidates(repo, repo.get_by_id("kv_new"))
        assert n == 0
        conn.close()

    def test_min_entity_overlap_constant_is_two(self):
        assert MIN_ENTITY_OVERLAP == 2


# ---------------------------------------------------------------------------
# Detector integration — run() inserts duplicate candidates
# ---------------------------------------------------------------------------


class TestRunPopulatesDuplicateStats:
    def test_run_records_duplicates_when_two_items_share_entities(
        self, tmp_path, monkeypatch
    ):
        from services.verification_detector.detector import run
        conn = _fresh_db(tmp_path, monkeypatch)

        # Mocked golden: two items in same domain sharing 2 entities
        golden = {
            "verifications": [
                {
                    "detection_type": "correction",
                    "title": "NPS uses rolling 90-day window",
                    "content": "NPS = rolling 90-day, not quarterly.",
                    "user_quote": "...",
                    "domain": "product",
                    "entities": ["NPS", "rolling-90"],
                    "base_confidence": 0.9,
                },
                {
                    "detection_type": "correction",
                    "title": "Updated NPS rolling 90-day formula",
                    "content": "NPS calculation uses rolling-90 day window.",
                    "user_quote": "...",
                    "domain": "product",
                    "entities": ["NPS", "rolling-90"],
                    "base_confidence": 0.9,
                },
            ],
        }
        extractor = MagicMock()
        extractor.extract_json.return_value = golden

        session_dir = tmp_path / "user_sessions" / "alice"
        session_dir.mkdir(parents=True)
        # Minimal valid JSONL transcript with at least one turn
        (session_dir / "s1.jsonl").write_text('{"role":"user","content":"hi"}\n')

        stats = run(conn, extractor, session_data_dir=tmp_path / "user_sessions")
        assert stats["items_created"] >= 1
        # The second item's duplicate-candidate hook should fire against the
        # first one (same entities, same domain).
        assert stats["duplicate_candidates_recorded"] >= 1
        conn.close()


# ---------------------------------------------------------------------------
# bulk_update partial-failure
# ---------------------------------------------------------------------------


class TestBulkUpdate:
    def test_bulk_update_returns_per_id_status(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        repo = KnowledgeRepository(conn)
        repo.create(id="kv_a", title="A", content="c", category="x")
        repo.create(id="kv_b", title="B", content="c", category="x")
        result = repo.bulk_update(
            ["kv_a", "kv_b", "kv_missing"],
            {"category": "engineering"},
        )
        assert result["kv_a"] == "updated"
        assert result["kv_b"] == "updated"
        assert result["kv_missing"] == "not_found"

        for item_id in ("kv_a", "kv_b"):
            it = repo.get_by_id(item_id)
            assert it["category"] == "engineering"
        conn.close()

    def test_bulk_update_tags_add_and_remove(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        repo = KnowledgeRepository(conn)
        repo.create(id="kv_a", title="A", content="c", category="x", tags=["t1", "t2"])
        repo.bulk_update(["kv_a"], {"tags_add": ["t3"], "tags_remove": ["t1"]})
        item = repo.get_by_id("kv_a")
        tags = item["tags"]
        if isinstance(tags, str):
            tags = json.loads(tags)
        assert "t3" in tags
        assert "t2" in tags
        assert "t1" not in tags
        conn.close()


# ---------------------------------------------------------------------------
# count_by_tag / count_by_audience
# ---------------------------------------------------------------------------


class TestStatsExtensions:
    def test_count_by_tag(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        repo = KnowledgeRepository(conn)
        repo.create(id="kv_a", title="a", content="c", category="x", tags=["t1", "t2"])
        repo.create(id="kv_b", title="b", content="c", category="x", tags=["t1"])
        out = repo.count_by_tag()
        assert out.get("t1") == 2
        assert out.get("t2") == 1
        conn.close()

    def test_count_by_audience_buckets_null_as_all(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        repo = KnowledgeRepository(conn)
        repo.create(id="kv_a", title="a", content="c", category="x")
        repo.create(id="kv_b", title="b", content="c", category="x")
        repo.update("kv_b", audience="group:finance")
        out = repo.count_by_audience()
        assert out.get("all", 0) >= 1
        assert out.get("group:finance") == 1
        conn.close()
