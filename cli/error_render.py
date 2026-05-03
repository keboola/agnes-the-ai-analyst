"""Shared CLI renderer for HTTP error responses.

Three CLI paths surface BigQuery / guardrail / RBAC typed errors today:
- ``da query --remote`` (POST /api/query)
- ``da query --register-bq`` (RemoteQueryEngine wrapping BqAccessError)
- ``da fetch`` / ``da schema`` etc. (cli.v2_client wrappers around v2 endpoints)

All three previously flattened the structured ``detail`` JSON to a
truncated single-line string, hiding the operator-facing hint that
explains how to fix ``USER_PROJECT_DENIED`` / cost-cap rejection /
unregistered ``bq.*`` paths. This module recognizes a few canonical
shapes and pretty-prints them; falls back to truncated form for
anything unrecognized so the renderer never makes a worse-than-
status-quo error message.

Closes #160 §4.7.
"""
from __future__ import annotations

import json
from textwrap import fill
from typing import Any


# Keys that hold long human-readable text — wrap separately so the line
# break is at a word boundary, not mid-key.
_WRAP_KEYS = ("hint", "suggestion")
# Keys to render first in the key/value block (when present); other keys
# follow in declaration order so a future server-side detail addition
# surfaces automatically without a renderer change.
_PRIORITY_KEYS = ("kind", "reason", "path", "registered_as",
                  "billing_project", "data_project", "scan_bytes",
                  "limit_bytes", "tables", "current", "limit",
                  "retry_after_seconds")


def render_error(status_code: int, body: Any) -> str:
    """Format an HTTP error body for stderr.

    Recognized shapes (pretty-printed):
    - ``{"detail": {"kind": str, ...}}`` — typed BqAccessError
    - ``{"detail": {"reason": str, ...}}`` — guardrail / RBAC dicts
    Anything else: fallback ``f"HTTP {status_code}: {str(body)[:500]}"``.
    """
    detail = _detail_dict(body)
    if detail is not None and ("kind" in detail or "reason" in detail):
        return _format_dict(status_code, detail)
    if isinstance(body, dict) and isinstance(body.get("detail"), str):
        return f"HTTP {status_code}: {body['detail']}"
    text = str(body) if not isinstance(body, str) else body
    if len(text) > 500:
        text = text[:497] + "..."
    return f"HTTP {status_code}: {text}"


def _detail_dict(body: Any) -> dict | None:
    """Return ``body['detail']`` when it's a dict, else None."""
    if isinstance(body, dict):
        d = body.get("detail")
        if isinstance(d, dict):
            return d
    return None


def _format_dict(status_code: int, detail: dict) -> str:
    """Multi-line render of a recognized typed-error dict."""
    label = detail.get("kind") or detail.get("reason") or "error"
    lines: list[str] = [f"Error: {label} (HTTP {status_code})"]

    seen: set[str] = {"kind", "reason"}  # already in the label line
    # Priority keys first
    for key in _PRIORITY_KEYS:
        if key in seen:
            continue
        if key in detail and detail[key] not in (None, ""):
            lines.append(_kv_line(key, detail[key]))
            seen.add(key)

    # Anything else not already shown and not a wrap key
    for key, value in detail.items():
        if key in seen or key in _WRAP_KEYS:
            continue
        if value not in (None, ""):
            lines.append(_kv_line(key, value))
            seen.add(key)

    # Wrap keys last — they're the long human-readable explanation
    for key in _WRAP_KEYS:
        if key in detail and detail[key]:
            wrapped = fill(
                str(detail[key]),
                width=80,
                initial_indent=f"  {key}: ",
                subsequent_indent="    ",
            )
            lines.append(wrapped)
    return "\n".join(lines)


def _kv_line(key: str, value: Any) -> str:
    """Format one ``  key: value`` line. Lists join with comma; dicts
    json-encode (rare but defensive)."""
    if isinstance(value, list):
        rendered = ", ".join(str(v) for v in value) if value else "(empty)"
    elif isinstance(value, dict):
        rendered = json.dumps(value, default=str)
    elif value == "":
        rendered = "(empty)"
    else:
        rendered = str(value)
    return f"  {key}: {rendered}"
