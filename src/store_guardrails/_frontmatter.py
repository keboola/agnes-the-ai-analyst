"""YAML-ish frontmatter parsing shared between the upload endpoint and the
content guardrail.

Lives in ``src/`` so the guardrail module (which has no app dependency) can
import it without creating an ``src/ → app/`` cycle. The upload endpoint
re-imports from here.
"""

from __future__ import annotations

import re

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)


def parse_frontmatter(text: str) -> dict:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    body = m.group(1)
    out: dict = {}
    for line in body.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def frontmatter_body_offset(text: str) -> int:
    """Return the character offset where the body starts (after `---\\n`).

    Zero if the document has no frontmatter. Used by content_check to slice
    off the metadata block before measuring body length.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return 0
    return m.end()
