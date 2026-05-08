"""LLM security review — agentic verdict over the uploaded bundle.

Mirrors the corporate-memory extraction pattern: builds a prompt, calls
``StructuredExtractor.extract_json`` against a strict JSON schema, parses
the result. The model tier is configurable per-instance via
``guardrails.review_model`` (Haiku / Sonnet / Opus) — see
``app/instance_config.get_guardrails_review_model``.

Cost cap: single-shot, MAX_REVIEW_BYTES content payload, max_tokens
budget tuned for the schema. Retries inside the extractor handle
transient errors; a hard timeout (configured at the connector level)
bounds wall-clock cost.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

from connectors.llm.anthropic_provider import AnthropicExtractor
from connectors.llm.exceptions import (
    LLMError,
)
from .prompts import (
    REVIEW_JSON_SCHEMA,
    SYSTEM_PROMPT,
    build_review_prompt,
)

logger = logging.getLogger(__name__)

# Bound the response budget. The schema is small — findings list typically
# has 0–3 items — but allow headroom so the model doesn't truncate.
MAX_RESPONSE_TOKENS = 2000

# The prompt is single-shot user-content; we wrap SYSTEM_PROMPT into the
# user message because StructuredExtractor's interface predates a separate
# system-prompt slot. The corporate-memory service does the same.
def _full_prompt(user_payload: str) -> str:
    return f"{SYSTEM_PROMPT}\n\n---\n\n{user_payload}"


def review_bundle(
    plugin_dir: Path,
    *,
    type_: str,
    name: str,
    version: str,
    description: Optional[str],
    api_key: str,
    model: str,
) -> Dict[str, Any]:
    """Run the LLM review against the baked plugin tree.

    Returns a dict with the schema:
        ``{risk_level, summary, findings[], template_placeholders_found,
           reviewed_by_model, error}``

    ``error`` is set only when the LLM call itself failed — the runner
    surfaces this as ``status='review_error'`` and exposes a retry path
    in the admin UI. On success ``error`` is None and the schema fields
    are populated by the model.
    """
    user_payload = build_review_prompt(
        plugin_dir,
        type_=type_,
        name=name,
        version=version,
        description=description,
    )

    extractor = AnthropicExtractor(api_key=api_key, model=model)
    try:
        result = extractor.extract_json(
            prompt=_full_prompt(user_payload),
            max_tokens=MAX_RESPONSE_TOKENS,
            json_schema=REVIEW_JSON_SCHEMA,
            schema_name="store_guardrails_review",
        )
    except LLMError as e:
        # Bubble up as a structured error the runner can persist into
        # store_submissions.llm_findings — operators see the failure
        # type without having to grep logs.
        logger.warning(
            "LLM guardrail review failed for %s/%s@%s: %s",
            type_, name, version, type(e).__name__,
        )
        return {
            "risk_level": None,
            "summary": None,
            "findings": [],
            "template_placeholders_found": 0,
            "reviewed_by_model": model,
            "error": f"{type(e).__name__}: {e}",
        }
    except Exception as e:
        # Catch-all: AnthropicExtractor only translates the four transient
        # error classes into LLMError. Anything else (BadRequestError on
        # schema mismatch, NotFoundError on a stale model ID, library
        # bugs) would otherwise bubble through BackgroundTasks and land
        # the request in the unhandled-error path while the submission
        # row stays stuck in 'pending_llm' forever. Convert here so the
        # admin queue surfaces a real review_error with a retry button.
        logger.exception(
            "LLM guardrail review unexpected error for %s/%s@%s",
            type_, name, version,
        )
        return {
            "risk_level": None,
            "summary": None,
            "findings": [],
            "template_placeholders_found": 0,
            "reviewed_by_model": model,
            "error": f"{type(e).__name__}: {e}",
        }

    # Defensive: ensure the keys we rely on exist with sane defaults even
    # if the model returns the optional ones empty.
    return {
        "risk_level": result.get("risk_level") or "medium",
        "summary": result.get("summary") or "",
        "findings": result.get("findings") or [],
        "template_placeholders_found": int(result.get("template_placeholders_found") or 0),
        "reviewed_by_model": model,
        "error": None,
    }


def is_safe(verdict: Dict[str, Any]) -> bool:
    """Decide whether a review verdict permits publication.

    Pass condition: ``risk_level IN ('safe','low')`` AND no individual
    finding has severity ``high|critical``. We intentionally let
    ``medium`` findings through with a low-risk verdict — the model uses
    medium for "would benefit from review but no immediate exploit".
    Operators escalate to Sonnet/Opus if they want a stricter floor.
    """
    if verdict.get("error"):
        return False
    risk = (verdict.get("risk_level") or "").lower()
    if risk not in {"safe", "low"}:
        return False
    for finding in verdict.get("findings") or []:
        sev = (finding.get("severity") or "").lower()
        if sev in {"high", "critical"}:
            return False
    return True
