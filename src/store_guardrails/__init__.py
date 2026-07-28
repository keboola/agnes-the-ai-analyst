"""Flea-market upload guardrails — pre-publish check pipeline.

Wired into ``app/api/store.py``: every POST/PUT to ``/api/store/entities``
runs the inline checks here (manifest, static security, quality+templating)
and, on pass, schedules an async LLM security review. Outcomes land in
``store_submissions`` and surface in ``/admin/store/submissions``.

Public surface:
    * ``runner.run_inline_checks(plugin_dir, type_) -> InlineResult``
    * ``runner.run_llm_review(entity_id) -> LlmResult``
    * ``InlineResult.passed`` / ``.checks`` / ``.to_response_dict()``
    * ``LlmResult.passed`` / ``.findings`` / ``.reviewed_by_model``

See ``docs/STORE_GUARDRAILS.md`` for the operator-facing reference and
``tests/test_store_guardrails_inline.py`` for the failure-mode catalogue.
"""

from .runner import (
    InlineResult,
    LlmResult,
    run_inline_checks,
    run_llm_review,
)

__all__ = [
    "InlineResult",
    "LlmResult",
    "run_inline_checks",
    "run_llm_review",
]
