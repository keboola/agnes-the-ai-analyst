"""Quality + templating heuristics for uploaded bundles.

Soft check — never blocks publication on its own. Runs three things:

1. **Description sanity** — submission carries a ``description`` long
   enough to be useful in the flea-market browse UI.
2. **Doc-body sanity** — the primary doc (``SKILL.md`` / agent .md /
   plugin manifest description) is non-empty and at least minimally
   substantive. Catches lazy "TODO: write me" submissions.
3. **AI-slop heuristics** — flags literal placeholder leftovers
   (``lorem ipsum``, ``<INSERT_X_HERE>``, repeated `TODO:` lines) that
   suggest the uploader pasted a template without filling it in.

4. **Templating recommendation** — counts Jinja-style ``{{var}}`` tokens
   in the bundle. If zero, surfaces a non-blocking *recommendation* in
   the response payload encouraging the uploader to parameterize for
   first-use customization. This is a feature signal, not a defect.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional


_DESC_MIN_CHARS = 20
_DOC_MIN_CHARS = 200

_PLACEHOLDER_RE = re.compile(r"\{\{\s*[A-Za-z_][A-Za-z0-9_\-]*(?:\s*\|[^}]*)?\s*\}\}")

_SLOP_PATTERNS: List[tuple[str, re.Pattern[str]]] = [
    ("lorem_ipsum", re.compile(r"\blorem\s+ipsum\b", re.IGNORECASE)),
    ("insert_placeholder", re.compile(r"<\s*INSERT[_\s][A-Z0-9_]+[_\s]?HERE\s*>")),
    ("todo_floor", re.compile(r"^\s*TODO\s*:?\s*$", re.MULTILINE)),
]

_TEMPLATING_HINT = (
    "Consider adding {{...}} placeholders for user-specific values "
    "(project IDs, channel names, key contacts). Agnes will prompt the "
    "user to fill them in on first install — your skill becomes much "
    "more effective with parameterization."
)


def check(
    plugin_dir: Path,
    *,
    description: Optional[str],
) -> Dict[str, Any]:
    """Run quality + templating heuristics over the baked bundle.

    Returns:
        ``{"status": "pass"|"warn", "issues": [...],
           "template_placeholders": int,
           "template_recommendation": <str or None>}``.

    Status is never ``"fail"`` — quality issues are tracked but don't
    block publication on their own. The runner aggregates: if the upload
    has no other failures, the worst this check produces is a warning
    surface in the API response.
    """
    issues: List[str] = []

    if not description or len(description.strip()) < _DESC_MIN_CHARS:
        issues.append("description_too_short")

    primary_doc, doc_text = _find_primary_doc(plugin_dir)
    if primary_doc is None or not doc_text:
        issues.append("missing_primary_doc")
    elif len(doc_text.strip()) < _DOC_MIN_CHARS:
        issues.append("doc_too_short")

    # Slop heuristics scan every text file we'd reasonably read at install
    # time. Cheap — just regex over the bundle.
    slop_hits: List[str] = []
    placeholder_count = 0
    for path in plugin_dir.rglob("*.md"):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for label, regex in _SLOP_PATTERNS:
            if regex.search(text):
                slop_hits.append(f"{label}:{path.relative_to(plugin_dir).as_posix()}")
        placeholder_count += len(_PLACEHOLDER_RE.findall(text))

    # Also scan plugin.json / shell scripts for placeholders so a plugin
    # whose customization lives in its config still counts as parameterized.
    for ext in (".json", ".yaml", ".yml", ".sh", ".py", ".txt"):
        for path in plugin_dir.rglob(f"*{ext}"):
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            placeholder_count += len(_PLACEHOLDER_RE.findall(text))

    if slop_hits:
        issues.extend(slop_hits)

    template_recommendation = None
    if placeholder_count == 0:
        template_recommendation = _TEMPLATING_HINT

    return {
        "status": "pass" if not issues else "warn",
        "issues": issues,
        "template_placeholders": placeholder_count,
        "template_recommendation": template_recommendation,
    }


def _find_primary_doc(plugin_dir: Path) -> tuple[Optional[Path], str]:
    """Return (path, text) for the primary documentation file.

    Preference order: SKILL.md / skill.md anywhere in the tree → agent.md
    → README.md → any .md. Robust to the baked layout, where SKILL.md
    lives at ``plugin_dir/skills/<suffixed>/SKILL.md`` rather than the
    plugin root.
    """
    preferred_names = ("SKILL.md", "skill.md", "agent.md", "README.md")
    for name in preferred_names:
        for path in sorted(plugin_dir.rglob(name)):
            if path.is_file():
                try:
                    return path, path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    return path, ""
    # Fallback: first .md anywhere in the bundle.
    for path in sorted(plugin_dir.rglob("*.md")):
        if path.is_file():
            try:
                return path, path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return path, ""
    return None, ""
