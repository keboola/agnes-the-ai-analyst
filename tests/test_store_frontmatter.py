"""Frontmatter parser tests — real-YAML support (folded/literal blocks).

The line-based parser read ``description: >`` as the literal ``">"``,
so a perfectly good 500-char folded description failed the guardrail
floor with a misleading ``too_short``. The parser now feeds the block
through ``yaml.safe_load`` first and only falls back to the legacy
line-splitter when the block isn't valid YAML (so pre-existing
"YAML-ish" documents keep parsing exactly as before).
"""

from __future__ import annotations

from src.store_guardrails._frontmatter import (
    frontmatter_body_offset,
    parse_frontmatter,
)

_LONG = (
    "Generate a self-contained local documentation website that explains "
    "any git repository in business and technical terms."
)


class TestRealYaml:
    def test_folded_scalar_description(self):
        text = (
            "---\n"
            "name: repo-explainer\n"
            "description: >\n"
            "  Generate a self-contained local documentation website that explains\n"
            "  any git repository in business and technical terms.\n"
            "---\n\nBody.\n"
        )
        fm = parse_frontmatter(text)
        assert fm["name"] == "repo-explainer"
        assert fm["description"] == _LONG

    def test_literal_block_scalar(self):
        text = "---\nname: x\ndescription: |\n  Line one.\n  Line two.\n---\nBody.\n"
        fm = parse_frontmatter(text)
        assert fm["description"] == "Line one.\nLine two."

    def test_quoted_multiline_value(self):
        text = '---\nname: x\ndescription: "Spans\n  two lines with: a colon"\n---\nBody.\n'
        fm = parse_frontmatter(text)
        assert fm["description"] == "Spans two lines with: a colon"

    def test_value_with_colon_space_quoted(self):
        text = '---\ndescription: "Use when: things break badly"\n---\n'
        fm = parse_frontmatter(text)
        assert fm["description"] == "Use when: things break badly"

    def test_scalar_types_coerced_to_str(self):
        text = "---\nname: 123\nenabled: true\nweight: 1.5\n---\n"
        fm = parse_frontmatter(text)
        assert fm["name"] == "123"
        assert fm["enabled"] == "true"
        assert fm["weight"] == "1.5"

    def test_empty_value_is_empty_string(self):
        fm = parse_frontmatter("---\ndescription:\nname: x\n---\n")
        assert fm["description"] == ""


class TestLegacyFallback:
    """Documents that were parseable before must stay parseable."""

    def test_simple_key_values(self):
        fm = parse_frontmatter("---\nname: my-skill\ndescription: Something useful\n---\nBody")
        assert fm == {"name": "my-skill", "description": "Something useful"}

    def test_unquoted_colon_space_falls_back_to_line_parser(self):
        # `key: a: b` is invalid YAML — the legacy splitter's behavior
        # (split on first colon) is preserved via the fallback path.
        fm = parse_frontmatter("---\ndescription: Use when: things break\n---\n")
        assert fm["description"] == "Use when: things break"

    def test_comments_and_blank_lines_ignored(self):
        fm = parse_frontmatter("---\n# a comment\n\nname: x\n---\n")
        assert fm == {"name": "x"}

    def test_no_frontmatter(self):
        assert parse_frontmatter("Just a body, no fences.") == {}

    def test_non_dict_yaml_returns_empty(self):
        assert parse_frontmatter("---\n- a\n- b\n---\n") == {}

    def test_quotes_stripped(self):
        fm = parse_frontmatter("---\nname: 'quoted'\ndescription: \"also quoted\"\n---\n")
        assert fm == {"name": "quoted", "description": "also quoted"}


def test_body_offset_unchanged():
    text = "---\nname: x\n---\nBody starts here"
    assert text[frontmatter_body_offset(text) :].strip() == "Body starts here"
