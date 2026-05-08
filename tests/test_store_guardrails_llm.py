"""LLM-review wiring tests — Anthropic call mocked.

These tests cover the runner's persistence behavior on each verdict
shape (safe / risky / error). The actual prompt engineering lives in
``src/store_guardrails/prompts.py`` and is exercised at integration
time, not here.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

import duckdb
import pytest

from connectors.llm.exceptions import LLMTimeoutError
from src import db as src_db
from src.repositories.store_entities import StoreEntitiesRepository
from src.repositories.store_submissions import StoreSubmissionsRepository
from src.store_guardrails.runner import LlmResult, run_llm_review


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    src_db._system_db_conn = None
    src_db._system_db_path = None
    c = src_db.get_system_db()
    yield c
    c.close()


@pytest.fixture
def plugin_dir():
    d = Path(tempfile.mkdtemp(prefix="agnes_llm_test_"))
    (d / "SKILL.md").write_text("# Test\nbody " * 30)
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _seed_pending_submission(conn, plugin_dir: Path) -> tuple[str, str]:
    """Stage a store_entities row + a pending_llm submission.

    Returns ``(entity_id, submission_id)`` so the test can assert against
    final state.
    """
    from src.repositories.users import UserRepository
    UserRepository(conn).create(id="u1", email="alice@x.com", name="alice")
    ents = StoreEntitiesRepository(conn)
    ents.create(
        id="e1", owner_user_id="u1", owner_username="alice",
        type="skill", name="probe", description="probe skill",
        category=None, version="1.0.0", file_size=10,
        visibility_status="pending",
    )
    subs = StoreSubmissionsRepository(conn)
    sub_id = subs.create(
        submitter_id="u1", submitter_email="alice@x.com",
        type="skill", name="probe", version="1.0.0",
        status="pending_llm", entity_id="e1",
        inline_checks={"manifest": {"status": "pass"}},
    )
    return "e1", sub_id


# The runner closes its own cursor in `finally`. Hand it a *fresh* cursor
# each call (mirrors the production `get_system_db` behavior) so closing
# it doesn't poison the test's primary cursor used for assertions.
def _conn_factory(_unused):
    def _f():
        return src_db.get_system_db()
    return _f


# ---------------------------------------------------------------------------
# run_llm_review outcomes
# ---------------------------------------------------------------------------


class TestLlmReviewRunner:
    def test_safe_verdict_approves_entity(self, conn, plugin_dir):
        eid, sub_id = _seed_pending_submission(conn, plugin_dir)

        verdict = {
            "risk_level": "safe", "summary": "OK", "findings": [],
            "template_placeholders_found": 0, "reviewed_by_model": "claude-haiku-4-5-20251001",
            "error": None,
        }
        with patch(
            "src.store_guardrails.runner.llm_review.review_bundle",
            return_value=verdict,
        ):
            result = run_llm_review(
                sub_id, plugin_dir=plugin_dir,
                conn_factory=_conn_factory(conn),
                api_key_loader=lambda: "sk-test",
                model_loader=lambda: "claude-haiku-4-5-20251001",
            )

        assert isinstance(result, LlmResult)
        assert result.passed
        sub = StoreSubmissionsRepository(conn).get(sub_id)
        assert sub["status"] == "approved"
        ent = StoreEntitiesRepository(conn).get(eid)
        assert ent["visibility_status"] == "approved"

    def test_high_risk_verdict_blocks(self, conn, plugin_dir):
        eid, sub_id = _seed_pending_submission(conn, plugin_dir)

        verdict = {
            "risk_level": "high", "summary": "exfil",
            "findings": [{
                "severity": "high", "category": "exfiltration",
                "file": "run.py", "explanation": "ships token to remote",
                "fix_hint": "remove the POST",
            }],
            "template_placeholders_found": 0, "reviewed_by_model": "claude-haiku-4-5-20251001",
            "error": None,
        }
        with patch(
            "src.store_guardrails.runner.llm_review.review_bundle",
            return_value=verdict,
        ):
            run_llm_review(
                sub_id, plugin_dir=plugin_dir,
                conn_factory=_conn_factory(conn),
                api_key_loader=lambda: "sk-test",
                model_loader=lambda: "claude-haiku-4-5-20251001",
            )

        sub = StoreSubmissionsRepository(conn).get(sub_id)
        assert sub["status"] == "blocked_llm"
        ent = StoreEntitiesRepository(conn).get(eid)
        # Entity stays pending — not visible until override.
        assert ent["visibility_status"] == "pending"

    def test_low_risk_with_high_finding_blocks(self, conn, plugin_dir):
        """Pass condition requires BOTH risk_level<=low AND no high findings.
        A 'low' verdict with a high finding still blocks."""
        eid, sub_id = _seed_pending_submission(conn, plugin_dir)

        verdict = {
            "risk_level": "low", "summary": "mostly ok",
            "findings": [{
                "severity": "high", "category": "credentials",
                "file": "creds.py", "explanation": "key in source",
            }],
            "template_placeholders_found": 0, "reviewed_by_model": "claude-haiku-4-5-20251001",
            "error": None,
        }
        with patch(
            "src.store_guardrails.runner.llm_review.review_bundle",
            return_value=verdict,
        ):
            run_llm_review(
                sub_id, plugin_dir=plugin_dir,
                conn_factory=_conn_factory(conn),
                api_key_loader=lambda: "sk-test",
                model_loader=lambda: "claude-haiku-4-5-20251001",
            )

        sub = StoreSubmissionsRepository(conn).get(sub_id)
        assert sub["status"] == "blocked_llm"

    def test_medium_finding_with_safe_risk_passes(self, conn, plugin_dir):
        """Medium findings shouldn't block when overall risk is safe — that's
        the 'noise but no exploit' band the operator opted into when picking
        Haiku as the tier. Operators who want stricter pin Sonnet/Opus."""
        eid, sub_id = _seed_pending_submission(conn, plugin_dir)

        verdict = {
            "risk_level": "safe", "summary": "noise",
            "findings": [{
                "severity": "medium", "category": "code_quality",
                "file": "x.py", "explanation": "could be cleaner",
            }],
            "template_placeholders_found": 1, "reviewed_by_model": "claude-haiku-4-5-20251001",
            "error": None,
        }
        with patch(
            "src.store_guardrails.runner.llm_review.review_bundle",
            return_value=verdict,
        ):
            run_llm_review(
                sub_id, plugin_dir=plugin_dir,
                conn_factory=_conn_factory(conn),
                api_key_loader=lambda: "sk-test",
                model_loader=lambda: "claude-haiku-4-5-20251001",
            )

        sub = StoreSubmissionsRepository(conn).get(sub_id)
        assert sub["status"] == "approved"

    def test_review_error_keeps_pending(self, conn, plugin_dir):
        eid, sub_id = _seed_pending_submission(conn, plugin_dir)

        # llm_review.review_bundle catches LLMError and returns a dict with
        # error set; the runner records review_error.
        verdict = {
            "risk_level": None, "summary": None, "findings": [],
            "template_placeholders_found": 0, "reviewed_by_model": "claude-haiku-4-5-20251001",
            "error": "LLMTimeoutError: Anthropic connection error",
        }
        with patch(
            "src.store_guardrails.runner.llm_review.review_bundle",
            return_value=verdict,
        ):
            run_llm_review(
                sub_id, plugin_dir=plugin_dir,
                conn_factory=_conn_factory(conn),
                api_key_loader=lambda: "sk-test",
                model_loader=lambda: "claude-haiku-4-5-20251001",
            )

        sub = StoreSubmissionsRepository(conn).get(sub_id)
        assert sub["status"] == "review_error"
        ent = StoreEntitiesRepository(conn).get(eid)
        assert ent["visibility_status"] == "pending"

    def test_missing_plugin_dir_records_review_error(self, conn, tmp_path):
        eid, sub_id = _seed_pending_submission(conn, tmp_path / "exists")
        # Point at a path that doesn't exist.
        ghost = tmp_path / "ghost-plugin-dir"
        with patch(
            "src.store_guardrails.runner.llm_review.review_bundle"
        ) as m:
            run_llm_review(
                sub_id, plugin_dir=ghost,
                conn_factory=_conn_factory(conn),
                api_key_loader=lambda: "sk-test",
                model_loader=lambda: "claude-haiku-4-5-20251001",
            )
            m.assert_not_called()

        sub = StoreSubmissionsRepository(conn).get(sub_id)
        assert sub["status"] == "review_error"

    def test_config_loader_failure_records_review_error(self, conn, plugin_dir):
        eid, sub_id = _seed_pending_submission(conn, plugin_dir)

        def boom():
            raise RuntimeError("no api key")

        run_llm_review(
            sub_id, plugin_dir=plugin_dir,
            conn_factory=_conn_factory(conn),
            api_key_loader=boom,
            model_loader=lambda: "claude-haiku-4-5-20251001",
        )

        sub = StoreSubmissionsRepository(conn).get(sub_id)
        assert sub["status"] == "review_error"


# ---------------------------------------------------------------------------
# llm_review.review_bundle — single-shot transport-error path
# ---------------------------------------------------------------------------


class TestReviewBundleErrorTransport:
    def test_anthropic_timeout_returns_error_dict(self, plugin_dir):
        from src.store_guardrails import llm_review

        with patch(
            "src.store_guardrails.llm_review.AnthropicExtractor"
        ) as MockEx:
            inst = MockEx.return_value
            inst.extract_json.side_effect = LLMTimeoutError("connection error")
            result = llm_review.review_bundle(
                plugin_dir, type_="skill", name="x", version="1.0.0",
                description="x" * 30,
                api_key="sk-test", model="claude-haiku-4-5-20251001",
            )
            assert result["error"]
            assert result["risk_level"] is None
            assert result["reviewed_by_model"] == "claude-haiku-4-5-20251001"
