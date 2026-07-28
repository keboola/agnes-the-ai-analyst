"""LLM-side helpers for the verification detector.

After the session-pipeline refactor, the orchestration loop (scan unprocessed
→ parse jsonl → mark processed) lives in services/session_pipeline/, and the
per-session persistence flow lives in services/session_processors/verification.py
(VerificationProcessor). This module retains only the pieces specific to LLM
extraction — prompt formatting, the structured-output call, and the
deterministic-id helper — which both the new processor and the legacy
__main__.py CLI shim still import.
"""

import hashlib
import logging

from connectors.llm import StructuredExtractor
from connectors.llm.exceptions import LLMError

from .prompts import VERIFICATION_EXTRACT_PROMPT
from .schemas import VERIFICATION_SCHEMA

logger = logging.getLogger(__name__)

MAX_TURNS_PER_SESSION = 100


def _generate_id(title: str, content: str) -> str:
    """Generate deterministic ID from title + content (same pattern as corporate memory collector)."""
    raw = f"{title}:{content}"
    return "kv_" + hashlib.sha256(raw.encode()).hexdigest()[:12]


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
