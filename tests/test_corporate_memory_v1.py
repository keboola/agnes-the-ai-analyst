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
    """Create a fresh DuckDB with the latest schema."""
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

    def test_schema_version_matches_constant(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.db import SCHEMA_VERSION, get_schema_version
        assert get_schema_version(conn) == SCHEMA_VERSION
        conn.close()

    def test_verification_evidence_table_exists(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
            ).fetchall()
        }
        assert "verification_evidence" in tables
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
        """Domain-only narrowing — topic matching is delegated to the LLM judge
        in services.corporate_memory.contradiction.find_and_judge (ADR D4)."""
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
        )
        ids = {c["id"] for c in candidates}
        # Only same-domain candidates are surfaced; the LLM does the rest.
        assert ids == {"k1", "k3"}
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
        # exponential: 0.90 * (0.5 ** (2/12)) ≈ 0.90 * 0.891 ≈ 0.802
        c = apply_decay(0.90, created)
        assert c < 0.90
        assert c == pytest.approx(0.90 * (0.5 ** (2.0 / 12.0)), abs=0.01)

    def test_decay_never_below_floor(self):
        from services.corporate_memory.confidence import apply_decay
        created = datetime.now(timezone.utc) - timedelta(days=3650)  # 10 years
        c = apply_decay(0.50, created)
        assert c >= 0.0

    def test_admin_mandate_decay_floor(self):
        from services.corporate_memory.confidence import apply_decay
        created = datetime.now(timezone.utc) - timedelta(days=3650)  # 10 years
        c = apply_decay(1.00, created, source_type="admin_mandate")
        assert c >= 0.50  # admin_mandate floor is 0.50

    def test_configure_overrides_defaults(self):
        from services.corporate_memory import confidence as cm
        original_base = dict(cm._BASE_CONFIDENCE)
        try:
            cm.configure({
                "base": {
                    "user_verification.correction": 0.75,
                },
                "decay": {
                    "mode": "exponential",
                    "half_life_months": 6,
                    "floor": {"admin_mandate": 0.60, "default": 0.0},
                },
            })
            c = cm.compute_confidence("user_verification", "correction")
            assert c == pytest.approx(0.75)
            created = datetime.now(timezone.utc) - timedelta(days=365)  # 12 months
            # exponential with half_life=6: 1.00 * (0.5 ** (12/6)) = 0.25, but floor=0.60
            c2 = cm.apply_decay(1.00, created, source_type="admin_mandate")
            assert c2 >= 0.60
        finally:
            # Restore defaults so other tests are not affected
            cm._BASE_CONFIDENCE = original_base
            cm._DECAY_CONFIG["floor"]["admin_mandate"] = 0.50


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
    """Batched contradiction detection (ADR Decision 4): one Haiku call returns
    judgments for every same-domain candidate, including structured
    suggested_resolution.
    """

    @staticmethod
    def _judgment(candidate_id, *, contradicts=False, severity=None,
                  explanation="", action=None, merged=None, justification=None):
        return {
            "candidate_id": candidate_id,
            "is_contradiction": contradicts,
            "severity": severity,
            "explanation": explanation,
            "resolution_action": action,
            "resolution_merged_content": merged,
            "resolution_justification": justification,
        }

    def test_contradiction_detected(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository
        from services.corporate_memory.contradiction import check_contradictions

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
            "judgments": [self._judgment(
                "k_existing",
                contradicts=True,
                severity="hard",
                explanation="customer-count vs revenue-based",
                action="kept_a",
                justification="new item is more accurate",
            )],
        }

        contradictions = check_contradictions(extractor, new_item, repo)
        # Content rule (vibecoding): assert exact values, not just count.
        assert len(contradictions) == 1
        assert contradictions[0]["item_a_id"] == "k_new"
        assert contradictions[0]["item_b_id"] == "k_existing"
        assert contradictions[0]["severity"] == "hard"
        assert contradictions[0]["suggested_resolution"]["action"] == "kept_a"
        # Single batched call — not one per candidate.
        assert extractor.extract_json.call_count == 1
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
            "judgments": [self._judgment(
                "k_existing", contradicts=False,
                explanation="Different aspects of NPS",
            )],
        }

        contradictions = check_contradictions(extractor, new_item, repo)
        assert contradictions == []
        # The Haiku call still happens — the *judgment* is what says no.
        assert extractor.extract_json.call_count == 1
        conn.close()

    def test_no_candidates_skips_llm(self, tmp_path, monkeypatch):
        """Cost guard: empty corpus → no Haiku call at all."""
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository
        from services.corporate_memory.contradiction import check_contradictions

        repo = KnowledgeRepository(conn)  # no items

        new_item = {
            "id": "k_new",
            "title": "Something new",
            "content": "Brand new knowledge",
            "domain": "finance",
        }

        extractor = MagicMock()
        contradictions = check_contradictions(extractor, new_item, repo)
        assert contradictions == []
        extractor.extract_json.assert_not_called()
        conn.close()

    def test_detect_and_record_persists_structured_resolution(self, tmp_path, monkeypatch):
        """detect_and_record persists, and suggested_resolution round-trips
        as a dict (JSON-encoded in DB, decoded on read)."""
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
            "judgments": [self._judgment(
                "k_existing",
                contradicts=True,
                severity="hard",
                explanation="conflicting definitions",
                action="merge",
                merged="Churn rolls up both views; track both metrics.",
                justification="both useful at different reporting layers",
            )],
        }

        cids = detect_and_record(extractor, new_item, repo)
        assert len(cids) == 1

        contradictions = repo.list_contradictions(resolved=False)
        assert len(contradictions) == 1
        c = contradictions[0]
        assert c["severity"] == "hard"
        # suggested_resolution round-trips as a structured dict.
        res = c["suggested_resolution"]
        assert isinstance(res, dict)
        assert res["action"] == "merge"
        assert res["merged_content"].startswith("Churn rolls up")
        assert "both useful" in res["justification"]
        conn.close()


# ===========================================================================
# Regression tests — pd-ps review (V1 must-fix)
# ===========================================================================

class TestContradictionCandidateSqlNarrowing:
    """Repository candidate narrowing (ADR Decision 4).

    Domain is the only SQL narrowing applied. Topic / content matching is
    delegated to Haiku in services.corporate_memory.contradiction.
    """

    def test_domain_excludes_other_domain(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository
        repo = KnowledgeRepository(conn)

        repo.create(id="k_finance", title="Churn is MRR-based", content="finance fact",
                    category="x", domain="finance", status="approved")
        repo.create(id="k_data", title="Churn pipeline doc", content="data fact",
                    category="x", domain="data", status="approved")

        candidates = repo.find_contradiction_candidates(
            new_item_id="k_new",
            domain="finance",
        )
        assert {c["id"] for c in candidates} == {"k_finance"}
        conn.close()

    def test_no_domain_returns_all_approved_items(self, tmp_path, monkeypatch):
        """When the new item has no domain, all approved/mandatory/pending
        items are surfaced — the LLM does the narrowing."""
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository
        repo = KnowledgeRepository(conn)
        repo.create(id="k1", title="A", content="a", category="x", status="approved")
        repo.create(id="k2", title="B", content="b", category="x", status="pending")
        repo.create(id="k3", title="C", content="c", category="x", status="rejected")

        candidates = repo.find_contradiction_candidates(new_item_id="k_new")
        ids = {c["id"] for c in candidates}
        # rejected items are out; approved + pending stay in.
        assert ids == {"k1", "k2"}
        conn.close()

    def test_domain_only_returns_all_same_domain(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository
        repo = KnowledgeRepository(conn)
        repo.create(id="f1", title="Revenue", content="x", category="x",
                    domain="finance", status="approved")
        repo.create(id="f2", title="Margin",  content="y", category="x",
                    domain="finance", status="approved")
        repo.create(id="p1", title="Churn",   content="z", category="x",
                    domain="product", status="approved")
        candidates = repo.find_contradiction_candidates(new_item_id="k_new", domain="finance")
        assert {c["id"] for c in candidates} == {"f1", "f2"}
        conn.close()

    def test_self_id_excluded_from_candidates(self, tmp_path, monkeypatch):
        """An item never contradicts itself — id != new_item_id must be enforced."""
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository
        repo = KnowledgeRepository(conn)
        repo.create(id="self_id", title="Churn metric", content="x",
                    category="x", domain="finance", status="approved")
        repo.create(id="other",   title="Churn metric", content="y",
                    category="x", domain="finance", status="approved")
        candidates = repo.find_contradiction_candidates(
            new_item_id="self_id",
            domain="finance",
        )
        assert {c["id"] for c in candidates} == {"other"}
        conn.close()

    def test_personal_items_excluded_from_contradiction_candidates(self, tmp_path, monkeypatch):
        """Personal items must NOT enter the LLM prompt as candidates — the
        Haiku call is a read site that exfiltrates content to the external
        API, and the LLM can paraphrase personal content into the persisted
        knowledge_contradictions.suggested_resolution.merged_content. ADR
        Decision 1 ("hard privacy boundary, not a UI hint") applies here."""
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository
        repo = KnowledgeRepository(conn)
        repo.create(id="public",  title="Public def",  content="x",
                    category="x", domain="finance", status="approved",
                    is_personal=False)
        repo.create(id="private", title="Private def", content="confidential",
                    category="x", domain="finance", status="approved",
                    is_personal=True)

        candidates = repo.find_contradiction_candidates(
            new_item_id="k_new", domain="finance",
        )
        ids = {c["id"] for c in candidates}
        assert ids == {"public"}
        # Defense in depth: also confirm by content match — even if the SQL
        # changed shape, no row carrying "confidential" must come back.
        assert all("confidential" not in (c.get("content") or "") for c in candidates)
        conn.close()


class TestDetectorIgnoresLLMConfidence:
    """Q3: LLM-supplied base_confidence in golden must be ignored.

    Confidence is derived in code from (source_type, detection_type) — never
    from the LLM output, even if a malicious or hallucinating model returns one.
    """

    def test_llm_returned_base_confidence_is_overridden(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository
        from services.verification_detector.detector import run

        # Hostile golden: LLM tries to claim confidence=0.99 on a confirmation
        # (which should be 0.60 in code).
        hostile = {
            "verifications": [{
                "detection_type": "confirmation",
                "title": "Hostile claim",
                "content": "LLM-elevated content",
                "user_quote": "yep",
                "domain": "engineering",
                "entities": [],
                "base_confidence": 0.99,
            }]
        }
        extractor = _mock_extractor(hostile)

        session_dir = tmp_path / "user_sessions" / "mallory"
        session_dir.mkdir(parents=True)
        (session_dir / "s.jsonl").write_text(
            json.dumps({"role": "user", "content": "yep"}) + "\n"
        )

        run(conn, extractor, session_data_dir=tmp_path / "user_sessions")

        repo = KnowledgeRepository(conn)
        items = repo.list_items(source_type="user_verification")
        assert len(items) == 1
        # 0.60 is the canonical (user_verification, confirmation) value, not
        # the 0.99 the LLM tried to inject.
        assert items[0]["confidence"] == 0.60
        # Belt-and-suspenders: the LLM-supplied base_confidence must never
        # round-trip onto the persisted item. If a future code change
        # reintroduces a base_confidence read path (e.g. into a new metadata
        # JSON column), this assertion will catch it.
        assert "base_confidence" not in items[0]
        conn.close()

    def test_unknown_detection_type_falls_back_to_canonical_value(self, tmp_path, monkeypatch):
        """If the LLM hallucinates a detection_type, fall back to the canonical
        (user_verification, confirmation) baseline rather than crashing or
        accepting an LLM-supplied number."""
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository
        from services.verification_detector.detector import run

        hallucinated = {
            "verifications": [{
                "detection_type": "totally_made_up_type",
                "title": "Hostile claim",
                "content": "x",
                "user_quote": "y",
                "domain": "engineering",
                "entities": [],
            }]
        }
        extractor = _mock_extractor(hallucinated)

        session_dir = tmp_path / "user_sessions" / "mallory"
        session_dir.mkdir(parents=True)
        (session_dir / "s.jsonl").write_text(
            json.dumps({"role": "user", "content": "y"}) + "\n"
        )

        run(conn, extractor, session_data_dir=tmp_path / "user_sessions")

        repo = KnowledgeRepository(conn)
        items = repo.list_items(source_type="user_verification")
        assert len(items) == 1
        assert items[0]["confidence"] == 0.60  # canonical confirmation fallback
        conn.close()


class TestDetectorPersistsEvidence:
    """Q3: user_quote and detection_type must land in verification_evidence."""

    def test_evidence_row_created_per_verification(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository
        from services.verification_detector.detector import run

        golden = _load_golden("correction_churn_metric")
        extractor = _mock_extractor(golden)

        session_dir = tmp_path / "user_sessions" / "alice"
        session_dir.mkdir(parents=True)
        import shutil
        shutil.copy(SESSIONS_DIR / "correction_churn_metric.jsonl", session_dir / "s1.jsonl")

        run(conn, extractor, session_data_dir=tmp_path / "user_sessions")

        repo = KnowledgeRepository(conn)
        items = repo.list_items(source_type="user_verification")
        assert len(items) == 1
        evidence = repo.list_evidence(items[0]["id"])
        assert len(evidence) == 1
        assert evidence[0]["detection_type"] == "correction"
        assert evidence[0]["source_user"] == "alice"
        # source_ref pins the evidence back to the originating session.
        assert evidence[0]["source_ref"] is not None
        assert "alice" in (evidence[0]["source_ref"] or "")
        # The LLM extracts the exact quote — that signal must persist.
        assert "MRR" in (evidence[0]["user_quote"] or "")
        conn.close()

    def test_mixed_session_creates_one_evidence_row_per_verification(self, tmp_path, monkeypatch):
        """Two verifications in one session → two distinct evidence rows on
        their respective items. Each row carries its own user_quote."""
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository
        from services.verification_detector.detector import run

        golden = _load_golden("mixed_session")
        extractor = _mock_extractor(golden)

        session_dir = tmp_path / "user_sessions" / "carol"
        session_dir.mkdir(parents=True)
        import shutil
        shutil.copy(SESSIONS_DIR / "mixed_session.jsonl", session_dir / "s.jsonl")

        run(conn, extractor, session_data_dir=tmp_path / "user_sessions")

        repo = KnowledgeRepository(conn)
        items = repo.list_items(source_type="user_verification")
        assert len(items) == 2
        # Each item gets exactly one evidence row, with the matching user_quote.
        all_quotes = []
        for item in items:
            evs = repo.list_evidence(item["id"])
            assert len(evs) == 1
            all_quotes.append(evs[0]["user_quote"])
        # Distinct quotes — confirms we are not stamping the same user_quote on
        # both items.
        assert len(set(all_quotes)) == 2
        conn.close()

    def test_duplicate_item_id_still_records_evidence(self, tmp_path, monkeypatch):
        """When two analysts independently produce the same (title, content),
        _generate_id collides and the second run hits the dedup `continue`
        path. ADR Decision 3 requires evidence to still accumulate so the
        second analyst's user_quote / detection_type / source_user are not
        silently dropped — that's what enables the "additional verifiers"
        boost mentioned in the ADR.
        """
        import shutil
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository
        from services.verification_detector.detector import run
        repo = KnowledgeRepository(conn)
        golden = _load_golden("correction_churn_metric")

        # Session 1 — alice. Creates the item + evidence row #1.
        alice_dir = tmp_path / "user_sessions" / "alice"
        alice_dir.mkdir(parents=True)
        shutil.copy(SESSIONS_DIR / "correction_churn_metric.jsonl", alice_dir / "s.jsonl")
        run(conn, _mock_extractor(golden), session_data_dir=tmp_path / "user_sessions")

        items = repo.list_items(source_type="user_verification")
        assert len(items) == 1
        item_id = items[0]["id"]
        evidence_after_alice = repo.list_evidence(item_id)
        assert len(evidence_after_alice) == 1
        assert evidence_after_alice[0]["source_user"] == "alice"

        # Session 2 — bob. Same golden output (same title+content → same
        # _generate_id), different session/user. Item already exists, but a
        # fresh evidence row must be persisted on the existing item.
        bob_dir = tmp_path / "user_sessions" / "bob"
        bob_dir.mkdir(parents=True)
        shutil.copy(SESSIONS_DIR / "correction_churn_metric.jsonl", bob_dir / "s.jsonl")
        run(conn, _mock_extractor(golden), session_data_dir=tmp_path / "user_sessions")

        # Item count unchanged.
        items = repo.list_items(source_type="user_verification")
        assert len(items) == 1
        assert items[0]["id"] == item_id

        # Evidence count grew — bob's evidence accumulated on the existing item.
        evidence_after_bob = repo.list_evidence(item_id)
        assert len(evidence_after_bob) == 2
        users = {e["source_user"] for e in evidence_after_bob}
        assert users == {"alice", "bob"}
        conn.close()


class TestDetectorWiresContradictionDetection:
    """Q2: detect_and_record() must run after repo.create() in the pipeline."""

    def test_contradiction_recorded_when_judge_says_yes(self, tmp_path, monkeypatch):
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository
        from services.verification_detector.detector import run
        from unittest.mock import MagicMock

        repo = KnowledgeRepository(conn)
        # Pre-existing approved item that the new one will conflict with.
        repo.create(
            id="existing", title="Churn definition",
            content="Customer-count based", category="business_logic",
            domain="finance", status="approved",
        )

        # Stub extractor: first call returns a verification; subsequent calls
        # (the contradiction judge) return contradicts=True.
        verification_response = {
            "verifications": [{
                "detection_type": "correction",
                "title": "Churn is MRR-based",
                "content": "Revenue-based",
                "user_quote": "MRR-based, not customer-count",
                "domain": "finance",
                "entities": ["churn", "MRR"],
            }]
        }
        contradiction_response = {
            "judgments": [{
                "candidate_id": "existing",
                "is_contradiction": True,
                "severity": "hard",
                "explanation": "definitions disagree",
                "resolution_action": "kept_a",
                "resolution_merged_content": None,
                "resolution_justification": "new item is more accurate",
            }]
        }

        extractor = MagicMock()
        extractor.extract_json.side_effect = [verification_response, contradiction_response]

        session_dir = tmp_path / "user_sessions" / "alice"
        session_dir.mkdir(parents=True)
        (session_dir / "s.jsonl").write_text(
            json.dumps({"role": "user", "content": "MRR-based, not customer-count"}) + "\n"
        )

        stats = run(conn, extractor, session_data_dir=tmp_path / "user_sessions")

        assert stats["items_created"] == 1
        assert stats["contradictions_recorded"] == 1
        contradictions = repo.list_contradictions(resolved=False)
        assert len(contradictions) == 1
        assert contradictions[0]["item_b_id"] == "existing"
        conn.close()

    def test_no_contradiction_when_judge_says_no(self, tmp_path, monkeypatch):
        """Judge returns contradicts=false → item still created, contradictions_recorded=0."""
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository
        from services.verification_detector.detector import run
        from unittest.mock import MagicMock

        repo = KnowledgeRepository(conn)
        repo.create(
            id="existing", title="Churn definition",
            content="Customer-count based", category="business_logic",
            domain="finance", status="approved",
        )

        extractor = MagicMock()
        extractor.extract_json.side_effect = [
            {
                "verifications": [{
                    "detection_type": "correction",
                    "title": "Churn refinement",
                    "content": "Same as existing, more detail",
                    "user_quote": "more detail",
                    "domain": "finance",
                    "entities": ["churn"],
                }]
            },
            {
                "judgments": [{
                    "candidate_id": "existing",
                    "is_contradiction": False,
                    "severity": None,
                    "explanation": "compatible — different scopes",
                    "resolution_action": None,
                    "resolution_merged_content": None,
                    "resolution_justification": None,
                }]
            },
        ]

        session_dir = tmp_path / "user_sessions" / "alice"
        session_dir.mkdir(parents=True)
        (session_dir / "s.jsonl").write_text(
            json.dumps({"role": "user", "content": "more detail"}) + "\n"
        )

        stats = run(conn, extractor, session_data_dir=tmp_path / "user_sessions")
        assert stats["items_created"] == 1
        assert stats["contradictions_recorded"] == 0
        assert repo.list_contradictions(resolved=False) == []
        conn.close()

    def test_contradiction_judge_failure_does_not_abort_run(self, tmp_path, monkeypatch):
        """If the contradiction judge raises LLMError, the item must still be
        created and the session must still be marked processed. Failure of the
        judge is degraded mode, not a fatal error."""
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository
        from services.verification_detector.detector import run
        from connectors.llm.exceptions import LLMError
        from unittest.mock import MagicMock

        repo = KnowledgeRepository(conn)
        repo.create(
            id="existing", title="Churn definition",
            content="x", category="business_logic",
            domain="finance", status="approved",
        )

        verification_response = {
            "verifications": [{
                "detection_type": "correction",
                "title": "Churn override",
                "content": "y",
                "user_quote": "z",
                "domain": "finance",
                "entities": ["churn"],
            }]
        }
        extractor = MagicMock()
        extractor.extract_json.side_effect = [
            verification_response,
            LLMError("simulated judge failure"),
        ]

        session_dir = tmp_path / "user_sessions" / "alice"
        session_dir.mkdir(parents=True)
        (session_dir / "s.jsonl").write_text(
            json.dumps({"role": "user", "content": "z"}) + "\n"
        )

        stats = run(conn, extractor, session_data_dir=tmp_path / "user_sessions")
        # Item lands; judge failure is logged and swallowed.
        assert stats["items_created"] == 1
        assert stats["contradictions_recorded"] == 0
        assert stats["sessions_processed"] == 1
        # Session is marked processed so we don't re-run on next sweep.
        assert repo.is_session_processed("alice/s.jsonl") is True
        conn.close()


class TestBatchedContradictionFindAndJudge:
    """Direct unit tests for find_and_judge — the new batched Haiku path
    (ADR Decision 4). Covers hallucination-defense, severity normalization,
    structured resolution shape, and the single-call cost guarantee.
    """

    @staticmethod
    def _judgment(candidate_id, *, contradicts=False, severity=None,
                  explanation="", action=None, merged=None, justification=None):
        return {
            "candidate_id": candidate_id,
            "is_contradiction": contradicts,
            "severity": severity,
            "explanation": explanation,
            "resolution_action": action,
            "resolution_merged_content": merged,
            "resolution_justification": justification,
        }

    def _seed(self, repo, n: int, domain: str = "finance"):
        ids = []
        for i in range(n):
            cid = f"c{i}"
            repo.create(
                id=cid, title=f"Item {i}", content=f"content {i}",
                category="business_logic", domain=domain, status="approved",
            )
            ids.append(cid)
        return ids

    def test_single_batched_call_for_many_candidates(self, tmp_path, monkeypatch):
        """Cost-shape guarantee: one extract_json call regardless of N
        candidates. Replaces the old N-call sequential pattern."""
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository
        from services.corporate_memory.contradiction import find_and_judge

        repo = KnowledgeRepository(conn)
        ids = self._seed(repo, 5)

        new_item = {"id": "k_new", "title": "New", "content": "x", "domain": "finance"}
        extractor = MagicMock()
        extractor.extract_json.return_value = {
            "judgments": [self._judgment(cid, contradicts=False) for cid in ids],
        }

        records = find_and_judge(extractor, new_item, repo)
        assert records == []
        # Single call regardless of corpus size — this is the whole point.
        assert extractor.extract_json.call_count == 1
        conn.close()

    def test_hallucinated_candidate_id_dropped(self, tmp_path, monkeypatch):
        """If Haiku returns a candidate_id that wasn't in the input list, drop
        it. Defends against schema-conformant but fabricated IDs."""
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository
        from services.corporate_memory.contradiction import find_and_judge

        repo = KnowledgeRepository(conn)
        self._seed(repo, 1)  # only c0 exists

        new_item = {"id": "k_new", "title": "New", "content": "x", "domain": "finance"}
        extractor = MagicMock()
        extractor.extract_json.return_value = {
            "judgments": [
                self._judgment("c0", contradicts=True, severity="hard",
                               explanation="real", action="kept_a",
                               justification="new wins"),
                self._judgment("hallucinated_id_that_does_not_exist",
                               contradicts=True, severity="hard",
                               explanation="fake", action="kept_a",
                               justification="should be dropped"),
            ],
        }

        records = find_and_judge(extractor, new_item, repo)
        assert len(records) == 1
        assert records[0]["item_b_id"] == "c0"
        conn.close()

    def test_mixed_batch_only_persists_contradictions(self, tmp_path, monkeypatch):
        """Three candidates, only one contradicts — only that one is recorded.
        Critical to confirm we don't store every judgment, only positives."""
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository
        from services.corporate_memory.contradiction import detect_and_record

        repo = KnowledgeRepository(conn)
        ids = self._seed(repo, 3)

        new_item = {"id": "k_new", "title": "New", "content": "x", "domain": "finance"}
        extractor = MagicMock()
        extractor.extract_json.return_value = {
            "judgments": [
                self._judgment(ids[0], contradicts=False, explanation="not related"),
                self._judgment(ids[1], contradicts=True, severity="soft",
                               explanation="possibly outdated", action="kept_a",
                               justification="new is more recent"),
                self._judgment(ids[2], contradicts=False, explanation="orthogonal"),
            ],
        }

        cids = detect_and_record(extractor, new_item, repo)
        assert len(cids) == 1
        contradictions = repo.list_contradictions(resolved=False)
        assert len(contradictions) == 1
        assert contradictions[0]["item_b_id"] == ids[1]
        assert contradictions[0]["severity"] == "soft"
        conn.close()

    def test_invalid_severity_normalized_to_none(self, tmp_path, monkeypatch):
        """A severity value outside {'hard', 'soft'} is normalized to None.
        Schema enum should already block this, but defense-in-depth."""
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository
        from services.corporate_memory.contradiction import find_and_judge

        repo = KnowledgeRepository(conn)
        self._seed(repo, 1)

        new_item = {"id": "k_new", "title": "New", "content": "x", "domain": "finance"}
        extractor = MagicMock()
        extractor.extract_json.return_value = {
            "judgments": [self._judgment(
                "c0", contradicts=True,
                severity="catastrophic",  # not in enum
                explanation="severe but unparseable",
                action="kept_a",
                justification="new wins",
            )],
        }

        records = find_and_judge(extractor, new_item, repo)
        assert len(records) == 1
        assert records[0]["severity"] is None

    def test_invalid_resolution_action_dropped(self, tmp_path, monkeypatch):
        """Unknown action is dropped from the persisted record (record stays,
        suggested_resolution is omitted)."""
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository
        from services.corporate_memory.contradiction import find_and_judge

        repo = KnowledgeRepository(conn)
        self._seed(repo, 1)

        new_item = {"id": "k_new", "title": "New", "content": "x", "domain": "finance"}
        extractor = MagicMock()
        extractor.extract_json.return_value = {
            "judgments": [self._judgment(
                "c0", contradicts=True, severity="hard",
                explanation="x",
                action="rewrite_from_scratch",  # not in enum
                justification="y",
            )],
        }

        records = find_and_judge(extractor, new_item, repo)
        assert len(records) == 1
        # Bad action means no resolution stored; the contradiction itself stays.
        assert "suggested_resolution" not in records[0]

    def test_merge_action_carries_merged_content(self, tmp_path, monkeypatch):
        """When action is 'merge', merged_content must persist on the
        suggested_resolution dict."""
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository
        from services.corporate_memory.contradiction import detect_and_record

        repo = KnowledgeRepository(conn)
        self._seed(repo, 1)

        new_item = {"id": "k_new", "title": "New", "content": "x", "domain": "finance"}
        extractor = MagicMock()
        extractor.extract_json.return_value = {
            "judgments": [self._judgment(
                "c0", contradicts=True, severity="soft",
                explanation="overlap", action="merge",
                merged="Both definitions co-exist; track separately and reconcile quarterly.",
                justification="non-conflicting scopes",
            )],
        }

        detect_and_record(extractor, new_item, repo)
        c = repo.list_contradictions(resolved=False)[0]
        res = c["suggested_resolution"]
        assert res["action"] == "merge"
        assert "co-exist" in res["merged_content"]
        conn.close()

    def test_legacy_string_resolution_still_readable(self, tmp_path, monkeypatch):
        """Backwards compat: rows persisted before ADR D4 carry plain-string
        suggested_resolution. Must continue to be readable as a string."""
        conn = _fresh_db(tmp_path, monkeypatch)
        from src.repositories.knowledge import KnowledgeRepository

        repo = KnowledgeRepository(conn)
        repo.create(id="a", title="x", content="x", category="x", status="approved")
        repo.create(id="b", title="y", content="y", category="x", status="approved")
        repo.create_contradiction(
            item_a_id="a", item_b_id="b",
            explanation="legacy",
            severity="hard",
            suggested_resolution="kept_a — see notes",  # plain string
        )
        c = repo.list_contradictions(resolved=False)[0]
        # Plain string is preserved as-is (not coerced into a dict).
        assert c["suggested_resolution"] == "kept_a — see notes"
        conn.close()


class TestExponentialDecayWithLinearFallback:
    """Verify exponential decay and linear fallback via configure()."""

    def test_linear_mode_still_works(self):
        from services.corporate_memory import confidence as cm
        orig = dict(cm._DECAY_CONFIG)
        try:
            cm.configure({"decay": {"mode": "linear", "decay_rate_monthly": 0.02, "floor": {"default": 0.0}}})
            created = datetime.now(timezone.utc) - timedelta(days=60)
            c = cm.apply_decay(0.90, created)
            assert c < 0.90
            assert c == pytest.approx(0.86, abs=0.01)
        finally:
            cm._DECAY_CONFIG.update(orig)
