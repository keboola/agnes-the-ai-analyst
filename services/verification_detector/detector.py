"""Main pipeline for the verification detector service.

Scans unprocessed analyst session transcripts, sends them to an LLM for
verification extraction, and stores the results in the knowledge repository.
"""

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from connectors.llm import StructuredExtractor
from connectors.llm.exceptions import LLMError
from src.repositories.knowledge import KnowledgeRepository

from .prompts import VERIFICATION_EXTRACT_PROMPT
from .schemas import VERIFICATION_SCHEMA

logger = logging.getLogger(__name__)

SESSION_DATA_DIR = Path(os.environ.get("SESSION_DATA_DIR", "/data/user_sessions"))
MAX_TURNS_PER_SESSION = 100


def _generate_id(title: str, content: str) -> str:
    """Generate deterministic ID from title + content (same pattern as corporate memory collector)."""
    raw = f"{title}:{content}"
    return "kv_" + hashlib.sha256(raw.encode()).hexdigest()[:12]


def scan_unprocessed_sessions(conn) -> list[tuple[str, Path]]:
    """Find JSONL files not yet in session_extraction_state table."""
    repo = KnowledgeRepository(conn)
    results: list[tuple[str, Path]] = []
    if not SESSION_DATA_DIR.exists():
        return results
    for user_dir in SESSION_DATA_DIR.iterdir():
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
    """Format conversation turns as readable text for the LLM prompt."""
    lines: list[str] = []
    for turn in turns:
        role = turn.get("role", "unknown")
        content = turn.get("content", "")
        lines.append(f"[{role}]: {content}")
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
    global SESSION_DATA_DIR
    if session_data_dir is not None:
        SESSION_DATA_DIR = session_data_dir

    stats: dict[str, Any] = {
        "sessions_scanned": 0,
        "sessions_processed": 0,
        "sessions_skipped": 0,
        "verifications_extracted": 0,
        "items_created": 0,
        "errors": [],
    }

    unprocessed = scan_unprocessed_sessions(conn)
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
                    repo.mark_session_processed(
                        session_key, username, 0, _compute_file_hash(jsonl_path)
                    )
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
                    logger.info("Duplicate item skipped: %s", item_id)
                    continue

                if not dry_run:
                    repo.create(
                        id=item_id,
                        title=v["title"],
                        content=v["content"],
                        category="business_logic",
                        source_user=username,
                        tags=v.get("entities", []),
                        status="pending",
                        confidence=v.get("base_confidence", 0.50),
                        domain=v.get("domain"),
                        entities=v.get("entities"),
                        source_type="user_verification",
                        source_ref=session_id,
                        sensitivity="internal",
                    )
                    items_created += 1

            if not dry_run:
                repo.mark_session_processed(
                    session_key, username, items_created, _compute_file_hash(jsonl_path)
                )

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
