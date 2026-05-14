"""VerificationProcessor — first plugin of the session-pipeline framework.

Wraps the body of the pre-refactor `verification_detector.detector.run()`
inner loop so the LLM extraction + persist behavior is unchanged after the
framework refactor. Tests in `tests/test_corporate_memory_v1.py` are the
regression contract.
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb

from connectors.llm import StructuredExtractor
from connectors.llm.exceptions import LLMError
from services.corporate_memory import contradiction as contradiction_module
from services.corporate_memory.confidence import compute_confidence
from services.session_pipeline.contract import ProcessorResult
from services.session_pipeline.lib import parse_jsonl
from services.verification_detector.duplicates import _record_duplicate_candidates
from services.verification_detector.detector import (
    _generate_id,
    extract_verifications,
)
from src.repositories.knowledge import KnowledgeRepository

logger = logging.getLogger(__name__)


class VerificationProcessor:
    name: str = "verification"
    cadence_minutes: int = 15

    def __init__(self, extractor: StructuredExtractor):
        self.extractor = extractor

    def process_session(
        self,
        session_path: Path,
        username: str,
        session_key: str,
        conn: duckdb.DuckDBPyConnection,
    ) -> ProcessorResult:
        repo = KnowledgeRepository(conn)
        session_id = f"session-{session_path.stem}-{username}"

        turns = parse_jsonl(session_path)
        if not turns:
            logger.info("Empty session: %s", session_key)
            return ProcessorResult(items_count=0)

        verifications = extract_verifications(self.extractor, username, session_id, turns)

        items_created = 0
        for v in verifications:
            item_id = _generate_id(v["title"], v["content"])
            existing = repo.get_by_id(item_id)
            if existing:
                # Hash collision on (title, content) → another analyst
                # produced the same fact. ADR Decision 3 expects multiple
                # evidence rows to accumulate (one per distinct
                # verification event), so we still persist the new
                # evidence row even though we skip the create+contradiction
                # path. Without this, the second analyst's user_quote and
                # detection_type are silently dropped and the
                # "additional verifiers" boost cannot accumulate.
                logger.info(
                    "Duplicate item — recording evidence on existing: %s",
                    item_id,
                )
                repo.create_evidence(
                    item_id=item_id,
                    source_user=username,
                    source_ref=session_id,
                    detection_type=v.get("detection_type"),
                    user_quote=v.get("user_quote"),
                )
                continue

            # Confidence is computed in code from (source_type, detection_type).
            # The LLM is not trusted to set its own credibility — see Q3 in
            # docs/archive/pd-ps-comments.md and the ADR.
            detection_type = v.get("detection_type")
            try:
                confidence_value = compute_confidence("user_verification", detection_type)
            except ValueError:
                # Unknown detection_type from the LLM; fall back to a
                # lookup-keyed default rather than the LLM-supplied value.
                confidence_value = compute_confidence("user_verification", "confirmation")
            repo.create(
                id=item_id,
                title=v["title"],
                content=v["content"],
                category="business_logic",
                source_user=username,
                tags=v.get("entities", []),
                status="pending",
                confidence=confidence_value,
                domain=v.get("domain"),
                entities=v.get("entities"),
                source_type="user_verification",
                source_ref=session_id,
                sensitivity="internal",
            )
            # Persist the verification evidence row — user_quote and
            # detection_type are the raw signal Bayesian re-calibration
            # will need later (Q3).
            repo.create_evidence(
                item_id=item_id,
                source_user=username,
                source_ref=session_id,
                detection_type=detection_type,
                user_quote=v.get("user_quote"),
            )
            items_created += 1

            # Record duplicate-candidate hints inline. Heuristic-only (no
            # LLM call) so it stays cheap; failures must never abort
            # session processing — log and continue. Issue #62.
            try:
                new_item = repo.get_by_id(item_id)
                if new_item is not None:
                    _record_duplicate_candidates(repo, new_item)
            except Exception as e:
                logger.warning(
                    "Duplicate-candidate detection failed for %s: %s",
                    item_id, e,
                )

            # Run contradiction detection inline. Failure of the LLM
            # judge must not abort session processing — log and move on.
            try:
                new_item = repo.get_by_id(item_id)
                if new_item is not None:
                    contradiction_module.detect_and_record(self.extractor, new_item, repo)
            except LLMError as e:
                logger.warning("Contradiction check failed for %s: %s", item_id, e)
            except Exception as e:
                logger.warning(
                    "Unexpected error during contradiction check for %s: %s",
                    item_id, e,
                )

        logger.info(
            "Processed %s: %d verifications, %d items created",
            session_key, len(verifications), items_created,
        )
        return ProcessorResult(items_count=items_created)


def build_verification_processor() -> VerificationProcessor:
    """Factory that constructs the LLM extractor from instance config + env.

    Mirrors the pattern in services/verification_detector/__main__.py and
    app/api/admin.py:run_verification_detector — both built the extractor
    lazily at call time. Raises if the LLM isn't configured."""
    from connectors.llm import create_extractor_from_env_or_config

    try:
        from app.instance_config import load_instance_config

        try:
            config = load_instance_config()
        except (ValueError, FileNotFoundError):
            config = {}
        ai_config = config.get("ai") if config else None
    except Exception:
        ai_config = None

    extractor = create_extractor_from_env_or_config(ai_config)
    return VerificationProcessor(extractor=extractor)
