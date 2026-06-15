"""Guard: .cowork-skills-note and .cowork-tool-card.is-passthrough must not
use hardcoded hex colours — only --ds-* tokens are permitted so the dark
theme remains readable (issue #656).
"""

from __future__ import annotations

import re
from pathlib import Path

TEMPLATE = (
    Path(__file__).parent.parent
    / "app" / "web" / "templates" / "me_cowork.html"
)

# Hex literals that appeared in the original broken implementation.
_BANNED = {
    "#f5f3ff",  # light-purple background
    "#c7d2fe",  # indigo-200 border
    "#5b21b6",  # violet-800 text
}

# Selectors whose rule-blocks are inspected; we extract the text between the
# opening `{` and the closing `}` for each selector.
_SELECTORS = (
    ".cowork-skills-note",
    ".cowork-tool-card.is-passthrough",
)


def _read_template() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def _extract_rule_body(css: str, selector: str) -> str:
    """Return the text inside the first rule block that starts with *selector*."""
    pattern = re.escape(selector) + r"\s*\{([^}]*)\}"
    m = re.search(pattern, css)
    return m.group(1) if m else ""


class TestCoworkDarkThemeTokens:
    """Structural CSS guard — no browser required."""

    def test_template_exists(self):
        assert TEMPLATE.exists(), f"Template not found: {TEMPLATE}"

    def test_cowork_skills_note_no_hardcoded_hex(self):
        source = _read_template()
        body = _extract_rule_body(source, ".cowork-skills-note")
        assert body, ".cowork-skills-note rule block not found in template"
        for hex_color in _BANNED:
            assert hex_color not in body, (
                f"Hardcoded colour {hex_color!r} found in .cowork-skills-note — "
                "use a --ds-* token instead so the dark theme is readable."
            )

    def test_cowork_tool_card_passthrough_no_hardcoded_hex(self):
        source = _read_template()
        body = _extract_rule_body(source, r".cowork-tool-card.is-passthrough")
        # The rule may be absent (removed entirely) or present without hex.
        if not body:
            return  # selector removed — no hex possible
        for hex_color in _BANNED:
            assert hex_color not in body, (
                f"Hardcoded colour {hex_color!r} found in "
                ".cowork-tool-card.is-passthrough — "
                "use a --ds-* token instead so the dark theme is readable."
            )

    def test_cowork_skills_note_uses_ds_surface_dim(self):
        """Background must resolve via the design-system token."""
        source = _read_template()
        body = _extract_rule_body(source, ".cowork-skills-note")
        assert "var(--ds-surface-dim)" in body, (
            ".cowork-skills-note background should use var(--ds-surface-dim)"
        )

    def test_cowork_skills_note_uses_ds_border(self):
        """Border must resolve via the design-system token."""
        source = _read_template()
        body = _extract_rule_body(source, ".cowork-skills-note")
        assert "var(--ds-border)" in body, (
            ".cowork-skills-note border should use var(--ds-border)"
        )

    def test_cowork_tool_card_passthrough_tool_name_no_hardcoded_hex(self):
        """The per-name override for .is-passthrough .tool-name must not hardcode hex."""
        source = _read_template()
        # Check the full combined selector block
        pattern = re.escape(".cowork-tool-card.is-passthrough .tool-name") + r"\s*\{([^}]*)\}"
        m = re.search(pattern, source)
        if not m:
            return  # rule removed entirely — OK
        body = m.group(1)
        for hex_color in _BANNED:
            assert hex_color not in body, (
                f"Hardcoded colour {hex_color!r} found in "
                ".cowork-tool-card.is-passthrough .tool-name"
            )
