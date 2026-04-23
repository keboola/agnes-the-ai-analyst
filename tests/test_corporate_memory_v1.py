"""Tests for Corporate Memory V1: verification detector, confidence, contradiction, entities.

Three-tier testing approach:
- Tier 1: Unit tests (no LLM, no mocking) — schema, parsing, confidence math, entity matching
- Tier 2: Integration tests (mocked LLM) — full pipelines with golden file responses
- Tier 3: Live LLM tests (CI-skippable) — marked with @pytest.mark.live_llm
"""

import json
import os
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import duckdb
import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SESSIONS_DIR = FIXTURES_DIR / "sessions"
VERIFICATIONS_DIR = FIXTURES_DIR / "verifications"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fresh_db(tmp_path, monkeypatch):
    """Create a fresh DuckDB with v8 schema."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    # Force re-creation of shared connection
    import src.db as db_module
    db_module._system_db_conn = None
    db_module._system_db_path = None
    conn = db_module.get_system_db()
    return conn


def _load_golden(name: str) -> dict:
    """Load a golden verification output file."""
    with open(VERIFICATIONS_DIR / f"{name}.json") as f:
        return json.load(f)


def _mock_extractor(golden_response: dict) -> MagicMock:
    """Create a mock StructuredExtractor that returns a golden response."""
    mock = MagicMock()
    mock.extract_json.return_value = golden_response
    return mock


# ===========================================================================
# TIER 1: Unit Tests (no LLM)
# ===========================================================================

class TestSchemaV8Migration:
    """Test DuckDB schema v7 -> v8 migration."""

    def test_fresh_install_has_v8_tables(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
            ).fetchall()
        }
        assert "knowledge_contradictions" in tables
        assert "session_extraction_state" in tables
        conn.close()

    def test_knowledge_items_has_new_columns(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        columns = {
            row[0]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'knowledge_items'"
            ).fetchall()
        }
        new_columns = {
            "confidence", "domain", "entities", "source_type", "source_ref",
            "valid_from", "valid_until", "supersedes", "sensitivity", "is_personal",
        }
        assert new_columns.issubset(columns), f"Missing: {new_columns - columns}"
        conn.close()

    def test_schema_version_is_8(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.db import get_schema_version
        assert get_schema_version(conn) == 8
        conn.close()


class TestKnowledgeRepositoryV1:
    """Test extended KnowledgeRepository with V1 fields."""

    def test_create_with_new_fields(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository
        repo = KnowledgeRepository(conn)

        repo.create(
            id="kv_test001",
            title="Test item",
            content="Test content",
            category="business_logic",
            source_user="analyst@test.com",
            confidence=0.90,
            domain="finance",
            entities=["churn", "MRR"],
            source_type="user_verification",
            source_ref="session-2026-04-22-analyst",
            sensitivity="internal",
        )

        item = repo.get_by_id("kv_test001")
        assert item is not None
        assert item["confidence"] == 0.90
        assert item["domain"] == "finance"
        assert item["source_type"] == "user_verification"
        assert item["source_ref"] == "session-2026-04-22-analyst"
        assert item["is_personal"] is False
        conn.close()

    def test_list_by_domain(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository
        repo = KnowledgeRepository(conn)

        repo.create(id="k1", title="A", content="a", category="x", domain="finance")
        repo.create(id="k2", title="B", content="b", category="x", domain="engineering")
        repo.create(id="k3", title="C", content="c", category="x", domain="finance")

        finance_items = repo.list_by_domain("finance")
        assert len(finance_items) == 2
        assert all(i["domain"] == "finance" for i in finance_items)
        conn.close()

    def test_set_personal_flag(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository
        repo = KnowledgeRepository(conn)

        repo.create(id="k1", title="A", content="a", category="x", source_user="me@test.com")
        repo.set_personal("k1", True)
        item = repo.get_by_id("k1")
        assert item["is_personal"] is True

        repo.set_personal("k1", False)
        item = repo.get_by_id("k1")
        assert item["is_personal"] is False
        conn.close()

    def test_exclude_personal_from_list(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository
        repo = KnowledgeRepository(conn)

        repo.create(id="k1", title="Public", content="a", category="x", is_personal=False)
        repo.create(id="k2", title="Personal", content="b", category="x", is_personal=True)

        all_items = repo.list_items(exclude_personal=False)
        assert len(all_items) == 2

        public_only = repo.list_items(exclude_personal=True)
        assert len(public_only) == 1
        assert public_only[0]["id"] == "k1"
        conn.close()

    def test_user_contributions(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository
        repo = KnowledgeRepository(conn)

        repo.create(id="k1", title="A", content="a", category="x", source_user="alice@test.com")
        repo.create(id="k2", title="B", content="b", category="x", source_user="bob@test.com")
        repo.create(id="k3", title="C", content="c", category="x", source_user="alice@test.com")

        alice_items = repo.get_user_contributions("alice@test.com")
        assert len(alice_items) == 2
        conn.close()

    def test_contradiction_crud(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository
        repo = KnowledgeRepository(conn)

        cid = repo.create_contradiction(
            item_a_id="k1", item_b_id="k2",
            explanation="They disagree on churn definition",
            severity="hard",
            suggested_resolution="k1 is more recent and verified",
        )
        assert cid.startswith("kc_")

        contradictions = repo.list_contradictions(resolved=False)
        assert len(contradictions) == 1
        assert contradictions[0]["item_a_id"] == "k1"

        repo.resolve_contradiction(cid, "admin@test.com", "kept_a")
        resolved = repo.list_contradictions(resolved=True)
        assert len(resolved) == 1
        assert resolved[0]["resolution"] == "kept_a"
        conn.close()

    def test_session_extraction_state(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository
        repo = KnowledgeRepository(conn)

        assert repo.is_session_processed("alice/session1.jsonl") is False

        repo.mark_session_processed("alice/session1.jsonl", "alice", 3, "abc123")
        assert repo.is_session_processed("alice/session1.jsonl") is True
        assert repo.is_session_processed("alice/session2.jsonl") is False
        conn.close()

    def test_find_contradiction_candidates(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository
        repo = KnowledgeRepository(conn)

        repo.create(id="k1", title="Churn is revenue-based", content="MRR churn",
                     category="x", domain="finance", status="approved")
        repo.create(id="k2", title="NPS calculation", content="Rolling 90 day",
                     category="x", domain="product", status="approved")
        repo.create(id="k3", title="Churn is customer-count based", content="Customer churn",
                     category="x", domain="finance", status="approved")

        candidates = repo.find_contradiction_candidates(
            new_item_id="k_new",
            domain="finance",
            title_words=["Churn"],
        )
        # Should find k1 and k3 (same domain + keyword match), not k2
        ids = {c["id"] for c in candidates}
        assert "k1" in ids
        assert "k3" in ids
        conn.close()


class TestConfidenceScoring:
    """Test confidence scoring module (pure math, no LLM)."""

    def test_correction_base_confidence(self):
        from services.corporate_memory.confidence import compute_confidence
        c = compute_confidence("user_verification", "correction")
        assert c == 0.90

    def test_confirmation_base_confidence(self):
        from services.corporate_memory.confidence import compute_confidence
        c = compute_confidence("user_verification", "confirmation")
        assert c == 0.60

    def test_unprompted_definition_base_confidence(self):
        from services.corporate_memory.confidence import compute_confidence
        c = compute_confidence("user_verification", "unprompted_definition")
        assert c == 0.90

    def test_admin_mandate_always_1(self):
        from services.corporate_memory.confidence import compute_confidence
        c = compute_confidence("admin_mandate")
        assert c == 1.00

    def test_claude_local_md_base(self):
        from services.corporate_memory.confidence import compute_confidence
        c = compute_confidence("claude_local_md")
        assert c == 0.50

    def test_multi_user_boost(self):
        from services.corporate_memory.confidence import boost_for_multi_verification
        # 2 additional verifiers = +0.10
        c = boost_for_multi_verification(0.90, verification_count=3)
        assert c == pytest.approx(1.00)  # 0.90 + 0.05*2 = 1.00

    def test_boost_capped_at_max(self):
        from services.corporate_memory.confidence import boost_for_multi_verification
        c = boost_for_multi_verification(0.95, verification_count=10)
        assert c == 1.00

    def test_decay_over_time(self):
        from services.corporate_memory.confidence import apply_decay
        created = datetime.now(timezone.utc) - timedelta(days=60)  # ~2 months
        c = apply_decay(0.90, created, decay_rate_monthly=0.02)
        assert c < 0.90
        assert c == pytest.approx(0.86, abs=0.01)

    def test_decay_never_below_zero(self):
        from services.corporate_memory.confidence import apply_decay
        created = datetime.now(timezone.utc) - timedelta(days=3650)  # 10 years
        c = apply_decay(0.50, created, decay_rate_monthly=0.02)
        assert c >= 0.0


class TestEntityResolution:
    """Test entity resolution v1 (string matching, no LLM)."""

    def test_basic_matching(self):
        from services.corporate_memory.entities import resolve_entities
        registry = {
            "metrics": ["churn", "MRR", "ARR"],
            "teams": ["engineering", "finance"],
        }
        matches = resolve_entities(
            content="Our churn metric uses MRR data from the finance team",
            title="Churn definition",
            entity_registry=registry,
        )
        assert "churn" in matches
        assert "MRR" in matches
        assert "finance" in matches
        assert "ARR" not in matches

    def test_case_insensitive(self):
        from services.corporate_memory.entities import resolve_entities
        registry = {"metrics": ["NPS"]}
        matches = resolve_entities(
            content="Our nps score is tracked weekly",
            title="NPS Tracking",
            entity_registry=registry,
        )
        assert "NPS" in matches

    def test_empty_registry(self):
        from services.corporate_memory.entities import resolve_entities
        matches = resolve_entities("some content", "some title", {})
        assert matches == []

    def test_resolve_and_merge(self):
        from services.corporate_memory.entities import resolve_and_merge
        registry = {"metrics": ["churn", "MRR"]}
        item = {
            "title": "Churn definition",
            "content": "Uses MRR data",
            "entities": ["existing_entity"],
        }
        merged = resolve_and_merge(item, registry)
        assert "existing_entity" in merged
        assert "churn" in merged
        assert "MRR" in merged

    def test_build_entity_registry(self):
        from services.corporate_memory.entities import build_entity_registry
        registry = build_entity_registry(
            groups={"engineering": {}, "finance": {}},
            entity_config={"metrics": ["churn", "MRR"]},
            metric_names=["revenue"],
        )
        assert "teams" in registry
        assert "engineering" in registry["teams"]
        assert "metrics" in registry
        assert "churn" in registry["metrics"]


class TestSessionParsing:
    """Test JSONL session file parsing (no LLM)."""

    def test_parse_correction_session(self):
        from services.verification_detector.detector import parse_session
        turns = parse_session(SESSIONS_DIR / "correction_churn_metric.jsonl")
        assert len(turns) == 4
        assert turns[0]["role"] == "assistant"
        assert turns[1]["role"] == "user"
        assert "wrong" in turns[1]["content"].lower()

    def test_parse_empty_file(self, tmp_path):
        from services.verification_detector.detector import parse_session
        empty_file = tmp_path / "empty.jsonl"
        empty_file.write_text("")
        turns = parse_session(empty_file)
        assert turns == []

    def test_parse_malformed_line_skipped(self, tmp_path):
        from services.verification_detector.detector import parse_session
        bad_file = tmp_path / "bad.jsonl"
        bad_file.write_text('{"role": "user", "content": "ok"}\nNOT_JSON\n{"role": "assistant", "content": "sure"}\n')
        turns = parse_session(bad_file)
        assert len(turns) == 2  # malformed line skipped


class TestVerificationIdGeneration:
    """Test deterministic ID generation."""

    def test_deterministic(self):
        from services.verification_detector.detector import _generate_id
        id1 = _generate_id("Churn metric", "MRR based")
        id2 = _generate_id("Churn metric", "MRR based")
        assert id1 == id2
        assert id1.startswith("kv_")

    def test_different_content_different_id(self):
        from services.verification_detector.detector import _generate_id
        id1 = _generate_id("Churn metric", "MRR based")
        id2 = _generate_id("Churn metric", "Customer based")
        assert id1 != id2


class TestSchemaValidation:
    """Validate golden files against VERIFICATION_SCHEMA without LLM."""

    def test_correction_golden_valid(self):
        import jsonschema
        from services.verification_detector.schemas import VERIFICATION_SCHEMA
        golden = _load_golden("correction_churn_metric")
        jsonschema.validate(golden, VERIFICATION_SCHEMA)

    def test_empty_golden_valid(self):
        import jsonschema
        from services.verification_detector.schemas import VERIFICATION_SCHEMA
        golden = _load_golden("no_verifications")
        jsonschema.validate(golden, VERIFICATION_SCHEMA)

    def test_mixed_golden_valid(self):
        import jsonschema
        from services.verification_detector.schemas import VERIFICATION_SCHEMA
        golden = _load_golden("mixed_session")
        jsonschema.validate(golden, VERIFICATION_SCHEMA)


# ===========================================================================
# TIER 2: Integration Tests (mocked LLM)
# ===========================================================================

class TestVerificationDetectorIntegration:
    """Full pipeline tests with mocked LLM extractor."""

    def test_correction_pipeline(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository
        from services.verification_detector.detector import run

        golden = _load_golden("correction_churn_metric")
        extractor = _mock_extractor(golden)

        # Setup session data
        session_dir = tmp_path / "user_sessions" / "alice"
        session_dir.mkdir(parents=True)
        import shutil
        shutil.copy(SESSIONS_DIR / "correction_churn_metric.jsonl", session_dir / "s1.jsonl")

        stats = run(conn, extractor, session_data_dir=tmp_path / "user_sessions")

        assert stats["sessions_processed"] == 1
        assert stats["verifications_extracted"] == 1
        assert stats["items_created"] == 1

        repo = KnowledgeRepository(conn)
        items = repo.list_items(source_type="user_verification")
        assert len(items) == 1
        assert items[0]["source_type"] == "user_verification"
        assert items[0]["domain"] == "finance"
        assert items[0]["confidence"] == 0.90
        conn.close()

    def test_empty_session_skipped(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        from services.verification_detector.detector import run

        golden = _load_golden("no_verifications")
        extractor = _mock_extractor(golden)

        session_dir = tmp_path / "user_sessions" / "bob"
        session_dir.mkdir(parents=True)
        import shutil
        shutil.copy(SESSIONS_DIR / "no_verifications.jsonl", session_dir / "s1.jsonl")

        stats = run(conn, extractor, session_data_dir=tmp_path / "user_sessions")

        assert stats["sessions_processed"] == 1
        assert stats["verifications_extracted"] == 0
        assert stats["items_created"] == 0
        conn.close()

    def test_idempotency(self, tmp_path, monkeypatch):
        """Running twice on same session should not create duplicate items."""
        conn = _fresh_db(tmp_path, monkeypatch)
        from services.verification_detector.detector import run

        golden = _load_golden("correction_churn_metric")
        extractor = _mock_extractor(golden)

        session_dir = tmp_path / "user_sessions" / "alice"
        session_dir.mkdir(parents=True)
        import shutil
        shutil.copy(SESSIONS_DIR / "correction_churn_metric.jsonl", session_dir / "s1.jsonl")

        # Run twice
        stats1 = run(conn, extractor, session_data_dir=tmp_path / "user_sessions")
        stats2 = run(conn, extractor, session_data_dir=tmp_path / "user_sessions")

        assert stats1["items_created"] == 1
        assert stats2["sessions_scanned"] == 0  # Already processed
        conn.close()

    def test_mixed_session_multiple_items(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository
        from services.verification_detector.detector import run

        golden = _load_golden("mixed_session")
        extractor = _mock_extractor(golden)

        session_dir = tmp_path / "user_sessions" / "carol"
        session_dir.mkdir(parents=True)
        import shutil
        shutil.copy(SESSIONS_DIR / "mixed_session.jsonl", session_dir / "s1.jsonl")

        stats = run(conn, extractor, session_data_dir=tmp_path / "user_sessions")

        assert stats["verifications_extracted"] == 2
        assert stats["items_created"] == 2

        repo = KnowledgeRepository(conn)
        items = repo.list_items(source_type="user_verification")
        assert len(items) == 2
        conn.close()

    def test_dry_run_no_writes(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository
        from services.verification_detector.detector import run

        golden = _load_golden("correction_churn_metric")
        extractor = _mock_extractor(golden)

        session_dir = tmp_path / "user_sessions" / "alice"
        session_dir.mkdir(parents=True)
        import shutil
        shutil.copy(SESSIONS_DIR / "correction_churn_metric.jsonl", session_dir / "s1.jsonl")

        stats = run(conn, extractor, dry_run=True, session_data_dir=tmp_path / "user_sessions")

        assert stats["verifications_extracted"] == 1
        assert stats["items_created"] == 0  # dry run

        repo = KnowledgeRepository(conn)
        items = repo.list_items(source_type="user_verification")
        assert len(items) == 0
        conn.close()


class TestContradictionDetectionIntegration:
    """Test contradiction detection with mocked LLM judge."""

    def test_contradiction_detected(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository
        from services.corporate_memory.contradiction import check_contradictions

        repo = KnowledgeRepository(conn)

        # Existing item
        repo.create(
            id="k_existing", title="Churn is customer-count based",
            content="Churn = customers lost / total customers",
            category="business_logic", domain="finance", status="approved",
        )

        new_item = {
            "id": "k_new",
            "title": "Churn is revenue-based",
            "content": "Churn = MRR lost / total MRR",
            "domain": "finance",
        }

        extractor = MagicMock()
        extractor.extract_json.return_value = {
            "contradicts": True,
            "explanation": "One says customer-count, other says revenue-based",
            "severity": "hard",
            "suggested_resolution": "k_new is more recent and verified by user",
        }

        contradictions = check_contradictions(extractor, new_item, repo)
        assert len(contradictions) == 1
        assert contradictions[0]["severity"] == "hard"
        conn.close()

    def test_no_contradiction(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository
        from services.corporate_memory.contradiction import check_contradictions

        repo = KnowledgeRepository(conn)
        repo.create(
            id="k_existing", title="NPS is measured quarterly",
            content="NPS survey every quarter",
            category="business_logic", domain="product", status="approved",
        )

        new_item = {
            "id": "k_new",
            "title": "NPS response rate",
            "content": "NPS has 40% response rate",
            "domain": "product",
        }

        extractor = MagicMock()
        extractor.extract_json.return_value = {
            "contradicts": False,
            "explanation": "Different aspects of NPS, no contradiction",
        }

        contradictions = check_contradictions(extractor, new_item, repo)
        assert len(contradictions) == 0
        conn.close()

    def test_no_candidates_skips_llm(self, tmp_path, monkeypatch):
        """When no candidates match domain/keywords, LLM should not be called."""
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository
        from services.corporate_memory.contradiction import check_contradictions

        repo = KnowledgeRepository(conn)
        # No existing items

        new_item = {
            "id": "k_new",
            "title": "Something new",
            "content": "Brand new knowledge",
            "domain": "finance",
        }

        extractor = MagicMock()
        contradictions = check_contradictions(extractor, new_item, repo)
        assert len(contradictions) == 0
        extractor.extract_json.assert_not_called()
        conn.close()

    def test_detect_and_record(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository
        from services.corporate_memory.contradiction import detect_and_record

        repo = KnowledgeRepository(conn)
        repo.create(
            id="k_existing", title="Churn is customer-count based",
            content="Churn = customers lost / total customers",
            category="business_logic", domain="finance", status="approved",
        )

        new_item = {
            "id": "k_new",
            "title": "Churn is revenue-based",
            "content": "Churn = MRR lost / total MRR",
            "domain": "finance",
        }

        extractor = MagicMock()
        extractor.extract_json.return_value = {
            "contradicts": True,
            "explanation": "Conflicting churn definitions",
            "severity": "hard",
            "suggested_resolution": "Use revenue-based",
        }

        cids = detect_and_record(extractor, new_item, repo)
        assert len(cids) == 1

        contradictions = repo.list_contradictions(resolved=False)
        assert len(contradictions) == 1
        assert contradictions[0]["severity"] == "hard"
        conn.close()
