"""Tests for the shared semantic template-assertion helper.

Lives alongside the helper so a broken helper fails its own tests, not
just every caller. Run as part of the normal `pytest` collection.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Mapping

import pytest


class ElementNotFound(AssertionError):
    """Raised when assert_element can't find a matching element."""


_WS_RE = re.compile(r"\s+")


def _collapse(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


class _ElementCollector(HTMLParser):
    """Walks the HTML and collects every open tag of `target_tag`.

    Each match is reported at OPEN time so a missing or misnested closing
    tag never hides an element from class/attr/href predicates — only
    `text=` matching needs the body, and that's collected by depth-tracked
    accumulation if/until a corresponding close arrives.

    Why a real parser instead of a regex: a lazy `<div>...</div>` regex
    consumes the FIRST inner `</div>` as the close, so an outer `<div>`
    "match" swallows any sibling/inner `<div>` that comes before the
    parser-correct closing tag. With nested layout containers (every real
    page has them) the inner element never even appears as an open-tag
    match. html.parser tracks depth correctly.
    """

    def __init__(self, target_tag: str) -> None:
        super().__init__(convert_charrefs=False)
        self._target = target_tag.lower()
        # Stack of dicts for any currently-open <target> element:
        #   {"text_parts": list[str], "depth": int, "match_idx": int}
        self._open_targets: list[dict] = []
        self._depth = 0
        # (attrs, text_parts_ref) pairs in document order. Body text is
        # accumulated via the mutable list reference, so the open-time
        # entry stays up-to-date as data arrives.
        self.matches: list[tuple[dict[str, str], list[str]]] = []

    def handle_starttag(self, tag, attrs):
        self._depth += 1
        if tag.lower() == self._target:
            attr_dict = {k: ("" if v is None else v) for k, v in attrs}
            text_parts: list[str] = []
            self.matches.append((attr_dict, text_parts))
            self._open_targets.append({
                "text_parts": text_parts,
                "depth": self._depth,
            })

    def handle_startendtag(self, tag, attrs):  # <input … /> style
        if tag.lower() == self._target:
            attr_dict = {k: ("" if v is None else v) for k, v in attrs}
            self.matches.append((attr_dict, []))

    def handle_endtag(self, tag):
        if (
            self._open_targets
            and tag.lower() == self._target
            and self._open_targets[-1]["depth"] == self._depth
        ):
            self._open_targets.pop()
        self._depth -= 1

    def handle_data(self, data):
        for entry in self._open_targets:
            entry["text_parts"].append(data)


def assert_element(
    html: str,
    tag: str,
    *,
    class_: str | None = None,
    href: str | None = None,
    text: str | None = None,
    attrs: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Find an element matching the predicate, return its attr dict.

    Required tokens (class_, attrs, href) are evaluated as a SUBSET:
    extra classes / attributes on the element don't break the match.
    Attribute order is irrelevant. `text` is a regex (case-sensitive)
    matched against the element's text content with whitespace collapsed.

    Raises ElementNotFound with a diagnostic message if no element matches.
    """
    required_classes = set((class_ or "").split())
    required_attrs = dict(attrs or {})
    if href is not None:
        required_attrs["href"] = href

    collector = _ElementCollector(tag)
    collector.feed(html)
    collector.close()

    for el_attrs, text_parts in collector.matches:
        el_classes = set(el_attrs.get("class", "").split())
        if required_classes and not required_classes.issubset(el_classes):
            continue
        if any(el_attrs.get(k) != v for k, v in required_attrs.items()):
            continue
        if text is not None and not re.search(text, _collapse("".join(text_parts))):
            continue
        return el_attrs

    raise ElementNotFound(
        f"no <{tag}> matched "
        f"class={sorted(required_classes)} attrs={required_attrs} "
        f"text={text!r}"
    )


# ---------- tests ----------


def test_assert_element_matches_attr_order_agnostic():
    html = '<a class="btn btn-primary" href="/x">Submit</a>'
    assert_element(html, "a", class_="btn btn-primary", href="/x", text="Submit")


def test_assert_element_matches_when_attrs_reordered():
    html = '<a href="/x" class="btn btn-primary">Submit</a>'
    assert_element(html, "a", class_="btn btn-primary", href="/x", text="Submit")


def test_assert_element_matches_class_subset():
    html = '<a class="btn-primary btn extra" href="/x">Submit</a>'
    assert_element(html, "a", class_="btn btn-primary", href="/x")


def test_assert_element_text_is_regex_with_whitespace_collapse():
    html = '<a class="btn" href="/x">\n  Submit a skill\n  or plugin\n</a>'
    assert_element(html, "a", class_="btn", href="/x",
                   text=r"Submit a skill or plugin")


def test_assert_element_raises_with_diagnostic_when_class_missing():
    html = '<a class="btn-secondary" href="/x">Submit</a>'
    with pytest.raises(ElementNotFound, match=r"class.*btn-primary"):
        assert_element(html, "a", class_="btn btn-primary", href="/x")


def test_assert_element_finds_nested_target_inside_outer_containers():
    """Regression: a lazy regex matcher swallows inner siblings via the
    outer container's match span. The parser tracks depth correctly."""
    html = '''<div id="page">
      <div id="content">
        <div class="guide-fastpath">
          <h3>title</h3>
          <p>body</p>
        </div>
      </div>
    </div>'''
    attrs = assert_element(html, "div", class_="guide-fastpath")
    assert attrs.get("class") == "guide-fastpath"


def test_assert_element_ignores_class_inside_style_block():
    """CSS rules like `.foo { … }` must not confuse the helper into
    matching a non-existent `<div class="foo">`. The parser is HTML-aware."""
    html = '''<style>.foo { color: red; }</style>
    <div class="bar">other</div>
    <div class="foo">target</div>'''
    attrs = assert_element(html, "div", class_="foo")
    assert attrs.get("class") == "foo"
