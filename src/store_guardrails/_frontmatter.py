"""Frontmatter parsing shared between the upload endpoint and the
content guardrail.

Lives in ``src/`` so the guardrail module (which has no app dependency) can
import it without creating an ``src/ → app/`` cycle. The upload endpoint
re-imports from here.

Parsing strategy: real YAML first (``yaml.safe_load``), so standard
multi-line forms — folded ``>`` / literal ``|`` block scalars, quoted
multi-line strings — resolve to their actual values instead of the first
line's fragment. Documents that are not valid YAML (e.g. an unquoted
``key: a: b`` line) fall back to the historical line-splitter, so
"YAML-ish" frontmatter that parsed before keeps parsing the same way.
"""

from __future__ import annotations

import re

import yaml

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)


def _coerce(value: object) -> str:
    """Render a YAML scalar the way the legacy string parser would have.

    ``None`` (empty value) → ``""``; booleans keep YAML's lowercase
    spelling; everything else stringifies (dates, ints, floats — and,
    defensively, lists/dicts, which no current consumer reads).
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value.strip()
    return str(value)


def _parse_lines(body: str) -> dict:
    """Legacy line-based fallback — split each line on the first colon."""
    out: dict = {}
    for line in body.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def parse_frontmatter(text: str) -> dict:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    body = m.group(1)
    try:
        data = yaml.safe_load(body)
    except yaml.YAMLError:
        return _parse_lines(body)
    if not isinstance(data, dict):
        # A YAML scalar/list isn't frontmatter; mirror the legacy parser,
        # which yielded {} for colon-less bodies.
        return _parse_lines(body)
    return {str(k).strip(): _coerce(v) for k, v in data.items()}


def frontmatter_body_offset(text: str) -> int:
    """Return the character offset where the body starts (after `---\\n`).

    Zero if the document has no frontmatter. Used by content_check to slice
    off the metadata block before measuring body length.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return 0
    return m.end()
