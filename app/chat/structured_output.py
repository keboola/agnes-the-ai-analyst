"""Structured JSON output — `response_format: {"type": "json_schema", ...}`
(V1b Task 7, `docs/superpowers/specs/2026-07-21-agent-profiles-and-agent-api
-design.md` §3).

**V2 note (not implemented here):** this is prompt-steering +
post-validation, NOT constrained decoding. There is no
`send_user_message(response_format=...)` param on the chat runtime (see
`app/chat/manager.py`) that would push the schema into the model's sampler —
:func:`schema_directive` is appended to the plain-text prompt as an
instruction, and the model is free to ignore it. :func:`validate` is the
only enforcement: it runs AFTER the turn completes, parses the collected
`answer` as JSON (tolerating a fenced ```json block, since agents commonly
wrap structured output that way even when told not to), and checks it
against the JSON Schema. A future version with real constrained decoding
(grammar-based sampling at the provider/runtime level) would let
:func:`validate` degrade to a pure sanity check instead of the sole gate.

Callers (`app/api/agent_runtime.py`'s `/responses`, `app/worker/kinds.py`'s
`_run_agent_response`) are responsible for: appending
:func:`schema_directive`'s output to the prompt before the run, and calling
:func:`validate` on the resulting `answer` afterward.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional, Tuple

import jsonschema

#: Matches a fenced code block anywhere in the text (```json ... ``` or
#: plain ``` ... ```) — greedy-minimal so the FIRST fenced block wins, which
#: is what a single-JSON-value answer wrapped once in a fence looks like.
_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)```", re.DOTALL | re.IGNORECASE)


def _strip_fence(text: str) -> str:
    """Best-effort extraction of the JSON payload from ``text``: if a fenced
    code block is present anywhere, use its contents; otherwise use the
    whole (stripped) text as-is. Agents commonly wrap JSON answers in a
    ```json fence even when instructed to emit raw JSON only — this keeps
    that common case parseable instead of always failing validation on it.
    """
    match = _FENCE_RE.search(text or "")
    if match:
        return match.group(1).strip()
    return (text or "").strip()


def validate(answer: str, response_format: Optional[dict]) -> Tuple[bool, Any, Optional[str]]:
    """Validate ``answer`` against ``response_format``.

    ``response_format`` other than ``{"type": "json_schema", "schema": {...}}``
    (including ``None``) is a no-op pass-through: ``(True, None, None)`` —
    there is nothing to enforce, so the caller's normal (unvalidated) answer
    handling applies unchanged.

    For ``{"type": "json_schema", ...}``: parses ``answer`` as JSON
    (tolerating a fenced ```json block, see :func:`_strip_fence`) and
    validates the result against ``response_format["schema"]`` via
    ``jsonschema``. Returns ``(True, parsed_object, None)`` on success,
    ``(False, None, error_message)`` on either a JSON parse failure or a
    schema violation.
    """
    if not response_format or response_format.get("type") != "json_schema":
        return True, None, None

    schema = response_format.get("schema") or {}
    candidate = _strip_fence(answer)

    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as exc:
        return False, None, f"answer is not valid JSON: {exc}"

    try:
        jsonschema.validate(parsed, schema)
    except jsonschema.ValidationError as exc:
        return False, None, f"answer does not match schema: {exc.message}"
    except jsonschema.SchemaError as exc:
        return False, None, f"response_format schema is invalid: {exc.message}"

    return True, parsed, None


def schema_directive(response_format: dict) -> str:
    """Build the prompt-steering directive appended to the caller's input
    when ``response_format`` is a ``json_schema`` request — see the module
    docstring's V2 note for why this is steering, not enforcement."""
    schema = response_format.get("schema") or {}
    return (
        "Respond ONLY with a single JSON value that matches the JSON Schema below. "
        "Do not include any explanation, markdown code fences, or any text before or "
        "after the JSON — the entire response must be valid, schema-conforming JSON "
        "on its own.\n\nJSON Schema:\n" + json.dumps(schema, indent=2)
    )
