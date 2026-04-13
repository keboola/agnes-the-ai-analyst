"""Tests for Corporate Memory knowledge collector."""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Minimal mock LLM extractor
# ---------------------------------------------------------------------------

class MockLLMProvider:
    """A minimal mock for connectors.llm.StructuredExtractor."""

    def __init__(self, response: dict):
        self._response = response

    def extract_json(self, prompt: str, max_tokens: int, json_schema: dict, schema_name: str) -> dict:
        return self._response


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f)


# ---------------------------------------------------------------------------
# Tests for _generate_id
# ---------------------------------------------------------------------------

class TestGenerateId:
    def test_returns_km_prefix(self):
        from services.corporate_memory.collector import _generate_id
        item_id = _generate_id("hello world")
        assert item_id.startswith("km_")

    def test_deterministic(self):
        from services.corporate_memory.collector import _generate_id
        assert _generate_id("same") == _generate_id("same")

    def test_different_content_different_id(self):
        from services.corporate_memory.collector import _generate_id
        assert _generate_id("aaa") != _generate_id("bbb")


# ---------------------------------------------------------------------------
# Tests for _process_catalog_response (hash change / governance preservation)
# ---------------------------------------------------------------------------

class TestProcessCatalogResponse:
    def test_new_item_gets_generated_id(self):
        from services.corporate_memory.collector import _process_catalog_response
        response_items = [
            {
                "existing_id": None,
                "title": "Tip One",
                "content": "Always check the logs first.",
                "category": "debugging",
                "tags": ["logs"],
                "source_users": ["alice"],
            }
        ]
        result = _process_catalog_response(response_items, existing={"items": {}})
        assert len(result) == 1
        item_id, item = next(iter(result.items()))
        assert item_id.startswith("km_")
        assert item["title"] == "Tip One"
        assert item["status"] == "approved"  # default initial_status

    def test_existing_id_preserved(self):
        from services.corporate_memory.collector import _process_catalog_response
        existing = {
            "items": {
                "km_abc123": {
                    "id": "km_abc123",
                    "title": "Old Title",
                    "content": "Old content",
                    "category": "debugging",
                    "tags": [],
                    "source_users": ["bob"],
                    "extracted_at": "2026-01-01T00:00:00+00:00",
                    "status": "approved",
                    "approved_by": "admin",
                    "approved_at": "2026-01-02T00:00:00+00:00",
                    "mandatory_reason": None,
                    "audience": "all",
                    "review_by": None,
                    "edited_by": None,
                    "edited_at": None,
                }
            }
        }
        response_items = [
            {
                "existing_id": "km_abc123",
                "title": "Updated Title",
                "content": "New content",
                "category": "debugging",
                "tags": ["updated"],
                "source_users": ["bob"],
            }
        ]
        result = _process_catalog_response(response_items, existing=existing)
        assert "km_abc123" in result
        item = result["km_abc123"]
        assert item["title"] == "Updated Title"

    def test_governance_fields_preserved(self):
        from services.corporate_memory.collector import GOVERNANCE_FIELDS, _process_catalog_response
        existing = {
            "items": {
                "km_abc123": {
                    "id": "km_abc123",
                    "title": "T",
                    "content": "C",
                    "category": "workflow",
                    "tags": [],
                    "source_users": ["carol"],
                    "extracted_at": "2026-01-01T00:00:00+00:00",
                    "status": "approved",
                    "approved_by": "manager",
                    "approved_at": "2026-03-01T00:00:00+00:00",
                    "mandatory_reason": "Policy",
                    "audience": "team",
                    "review_by": "2026-12-31",
                    "edited_by": "carol",
                    "edited_at": "2026-02-01T00:00:00+00:00",
                }
            }
        }
        response_items = [
            {
                "existing_id": "km_abc123",
                "title": "T",
                "content": "C updated",
                "category": "workflow",
                "tags": [],
                "source_users": ["carol"],
            }
        ]
        result = _process_catalog_response(response_items, existing=existing)
        item = result["km_abc123"]
        assert item["approved_by"] == "manager"
        assert item["mandatory_reason"] == "Policy"
        assert item["audience"] == "team"

    def test_new_item_with_pending_initial_status(self):
        from services.corporate_memory.collector import _process_catalog_response
        response_items = [
            {
                "existing_id": None,
                "title": "Another tip",
                "content": "Some content",
                "category": "workflow",
                "tags": [],
                "source_users": ["dave"],
            }
        ]
        result = _process_catalog_response(
            response_items, existing={"items": {}}, initial_status="pending"
        )
        item = next(iter(result.values()))
        assert item["status"] == "pending"


# ---------------------------------------------------------------------------
# Tests for check_sensitivity
# ---------------------------------------------------------------------------

class TestCheckSensitivity:
    def test_safe_item_returns_true(self):
        from services.corporate_memory.collector import check_sensitivity
        extractor = MockLLMProvider({"safe": True})
        item = {"id": "km_x", "title": "T", "content": "C", "tags": []}
        assert check_sensitivity(extractor, item) is True

    def test_unsafe_item_returns_false(self):
        from services.corporate_memory.collector import check_sensitivity
        extractor = MockLLMProvider({"safe": False, "reason": "Contains PII"})
        item = {"id": "km_y", "title": "T", "content": "C", "tags": []}
        assert check_sensitivity(extractor, item) is False

    def test_llm_error_returns_false(self):
        """When the LLM raises an LLMError, the item is treated as unsafe."""
        from connectors.llm.exceptions import LLMError
        from services.corporate_memory.collector import check_sensitivity

        class ErrorExtractor:
            def extract_json(self, *args, **kwargs):
                raise LLMError("Network error")

        item = {"id": "km_z", "title": "T", "content": "C", "tags": []}
        assert check_sensitivity(ErrorExtractor(), item) is False


# ---------------------------------------------------------------------------
# Integration-style: collect_all with mocked I/O
# ---------------------------------------------------------------------------

class TestCollectAllSkipsWhenNoChanges:
    def test_skips_when_no_user_files(self, tmp_path):
        """collect_all returns skipped=True when no CLAUDE.local.md files exist."""
        from services.corporate_memory import collector

        with (
            patch.object(collector, "HOME_BASE", tmp_path / "home"),
            patch.object(collector, "KNOWLEDGE_FILE", tmp_path / "knowledge.json"),
            patch.object(collector, "USER_HASHES_FILE", tmp_path / "user_hashes.json"),
        ):
            (tmp_path / "home").mkdir()
            stats = collector.collect_all(dry_run=True)
            assert stats["skipped"] is True

    def test_skips_when_hashes_unchanged(self, tmp_path):
        """collect_all skips when hashes match stored values."""
        from services.corporate_memory import collector

        home = tmp_path / "home"
        home.mkdir()
        user_dir = home / "alice"
        user_dir.mkdir()
        claude_file = user_dir / "CLAUDE.local.md"
        claude_file.write_text("# My tips\n- Always document code")

        content = claude_file.read_text(encoding="utf-8")
        md5 = hashlib.md5(content.encode()).hexdigest()
        user_hashes_file = tmp_path / "user_hashes.json"
        _write_json(user_hashes_file, {"hashes": {"alice": md5}})

        with (
            patch.object(collector, "HOME_BASE", home),
            patch.object(collector, "KNOWLEDGE_FILE", tmp_path / "knowledge.json"),
            patch.object(collector, "USER_HASHES_FILE", user_hashes_file),
        ):
            stats = collector.collect_all(dry_run=True)
            assert stats["skipped"] is True
