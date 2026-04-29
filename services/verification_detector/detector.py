"""Main pipeline for the verification detector service.

Scans unprocessed analyst session transcripts, sends them to an LLM for
verification extraction, and stores the results in the knowledge repository.
"""

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

from connectors.llm import StructuredExtractor
from connectors.llm.exceptions import LLMError
from services.corporate_memory import contradiction as contradiction_module
from services.corporate_memory.confidence import compute_confidence
from src.repositories.knowledge import KnowledgeRepository

from .duplicates import _record_duplicate_candidates
from .prompts import VERIFICATION_EXTRACT_PROMPT
from .schemas import VERIFICATION_SCHEMA

logger = logging.getLogger(__name__)

SESSION_DATA_DIR = Path(os.environ.get("SESSION_DATA_DIR", "/data/user_sessions"))
MAX_TURNS_PER_SESSION = 100


def _generate_id(title: str, content: str) -> str:
    """Generate deterministic ID from title + content (same pattern as corporate memory collector)."""
    raw = f"{title}:{content}"
    return "kv_" + hashlib.sha256(raw.encode()).hexdigest()[:12]


def scan_unprocessed_sessions(conn, session_dir: Path | None = None) -> list[tuple[str, Path]]:
    """Find JSONL files not yet in session_extraction_state table."""
    repo = KnowledgeRepository(conn)
    results: list[tuple[str, Path]] = []
    effective_dir = session_dir if session_dir is not None else SESSION_DATA_DIR
    if not effective_dir.exists():
        return results
    for user_dir in effective_dir.iterdir():
        if not user_dir.is_dir():
            continue
        username = user_dir.name
        for jsonl_file in sorted(user_dir.glob("*.jsonl")):
            key = f"{username}/{jsonl_file.name}"
            if not repo.is_session_processed(key):
                results.append((username, jsonl_file))
    return results


def parse_session(jsonl_path: Path) -> list[dict]:
    """Parse JSONL session file into conversation turns."""
    turns: list[dict] = []
    with open(jsonl_path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    turns.append(json.loads(line))
                except json.JSONDecodeError:
                    logger.warning("Skipping malformed JSONL line in %s", jsonl_path)
    return turns


def _format_turns(turns: list[dict]) -> str:
    """Format conversation turns as a parseable, prompt-injection-hardened block.

    Session transcripts are heavily user-influenced (anything the analyst typed
    lands here). Each turn is wrapped in `<turn role="…">` tags with `</turn>`
    neutralized inside the content so a crafted message cannot break out of
    the wrapper. The trust-boundary instruction in VERIFICATION_EXTRACT_PROMPT
    tells the LLM to treat content inside `<turn>` as data, not directives.
    """
    lines: list[str] = []
    for turn in turns:
        role = turn.get("role", "unknown")
        content = (turn.get("content") or "").replace("</turn>", "&lt;/turn&gt;")
        lines.append(f'<turn role="{role}">{content}</turn>')
    return "\n".join(lines)


def _compute_file_hash(path: Path) -> str:
    """Compute MD5 hash of a file."""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def extract_verifications(
    extractor: StructuredExtractor,
    username: str,
    session_id: str,
    turns: list[dict],
    max_turns: int = MAX_TURNS_PER_SESSION,
) -> list[dict]:
    """Send conversation turns to LLM for verification detection."""
    if not turns:
        return []

    # Truncate to last N turns if too long
    if len(turns) > max_turns:
        turns = turns[-max_turns:]

    conversation_text = _format_turns(turns)
    prompt = VERIFICATION_EXTRACT_PROMPT.format(
        username=username,
        session_id=session_id,
        conversation=conversation_text,
    )

    try:
        result = extractor.extract_json(
            prompt=prompt,
            max_tokens=4096,
            json_schema=VERIFICATION_SCHEMA,
            schema_name="verification_extract",
        )
        return result.get("verifications", [])
    except LLMError as e:
        logger.error("LLM extraction failed for session %s: %s", session_id, e)
        return []


def run(
    conn,
    extractor: StructuredExtractor,
    dry_run: bool = False,
    session_data_dir: Path | None = None,
) -> dict[str, Any]:
    """Run the full verification detection pipeline.

    Returns stats dict with counts.
    """
    effective_session_dir = session_data_dir if session_data_dir is not None else SESSION_DATA_DIR

    stats: dict[str, Any] = {
        "sessions_scanned": 0,
        "sessions_processed": 0,
        "sessions_skipped": 0,
        "verifications_extracted": 0,
        "items_created": 0,
        "contradictions_recorded": 0,
        "duplicate_candidates_recorded": 0,
        "errors": [],
    }

    unprocessed = scan_unprocessed_sessions(conn, session_dir=effective_session_dir)
    stats["sessions_scanned"] = len(unprocessed)

    if not unprocessed:
        logger.info("No unprocessed sessions found")
        return stats

    repo = KnowledgeRepository(conn)

    for username, jsonl_path in unprocessed:
        session_key = f"{username}/{jsonl_path.name}"
        session_id = f"session-{jsonl_path.stem}-{username}"

        try:
            turns = parse_session(jsonl_path)
            if not turns:
                logger.info("Empty session: %s", session_key)
                if not dry_run:
                    repo.mark_session_processed(session_key, username, 0, _compute_file_hash(jsonl_path))
                stats["sessions_skipped"] += 1
                continue

            verifications = extract_verifications(extractor, username, session_id, turns)
            stats["verifications_extracted"] += len(verifications)

            items_created = 0
            for v in verifications:
                item_id = _generate_id(v["title"], v["content"])
                # Check if item already exists (deduplication)
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
                    if not dry_run:
                        repo.create_evidence(
                            item_id=item_id,
                            source_user=username,
                            source_ref=session_id,
                            detection_type=v.get("detection_type"),
                            user_quote=v.get("user_quote"),
                        )
                    continue

                if not dry_run:
                    # Confidence is computed in code from (source_type, detection_type).
                    # The LLM is not trusted to set its own credibility — see Q3 in
                    # docs/pd-ps-comments.md and the ADR.
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
                    # Record duplicate-candidate hints inline. Heuristic-only
                    # (no LLM call) so it stays cheap; failures must never
                    # abort session processing — log and continue. Issue #62.
                    try:
                        new_item = repo.get_by_id(item_id)
                        if new_item is not None:
                            recorded_dup = _record_duplicate_candidates(
                                repo, new_item
                            )
                            stats["duplicate_candidates_recorded"] += recorded_dup
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
                            recorded = contradiction_module.detect_and_record(extractor, new_item, repo)
                            stats["contradictions_recorded"] += len(recorded)
                    except LLMError as e:
                        logger.warning("Contradiction check failed for %s: %s", item_id, e)
                    except Exception as e:
                        logger.warning(
                            "Unexpected error during contradiction check for %s: %s",
                            item_id,
                            e,
                        )

            if not dry_run:
                repo.mark_session_processed(session_key, username, items_created, _compute_file_hash(jsonl_path))

            stats["sessions_processed"] += 1
            stats["items_created"] += items_created
            logger.info(
                "Processed %s: %d verifications, %d items created",
                session_key,
                len(verifications),
                items_created,
            )

        except Exception as e:
            logger.error("Error processing %s: %s", session_key, e)
            stats["errors"].append(f"{session_key}: {e}")

    return stats
