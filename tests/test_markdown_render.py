"""Tests for app/markdown_render.py — the curator-content render+sanitize path."""

from __future__ import annotations

from app.markdown_render import render_safe


# --- Empty / null inputs -------------------------------------------------


def test_render_safe_none_returns_empty_string():
    assert render_safe(None) == ""


def test_render_safe_empty_string_returns_empty_string():
    assert render_safe("") == ""


def test_render_safe_whitespace_only_returns_empty_or_trivial():
    # Whitespace-only markdown renders to "" or a single empty paragraph;
    # either is acceptable — both render as nothing visually.
    out = render_safe("   \n   ")
    assert out.strip() in {"", "<p></p>"}


# --- Allowed formatting survives -----------------------------------------


def test_render_safe_renders_paragraph_with_bold():
    assert render_safe("**hello** world").strip() == "<p><strong>hello</strong> world</p>"


def test_render_safe_renders_italic():
    assert "<em>x</em>" in render_safe("*x*")


def test_render_safe_renders_inline_code():
    assert "<code>foo()</code>" in render_safe("`foo()`")


def test_render_safe_renders_fenced_code_block():
    out = render_safe("```py\nprint('hi')\n```")
    assert "<pre>" in out
    assert "<code>" in out
    # The literal `print('hi')` must end up inside <code> — apostrophe is
    # safe in attribute-free body text so escaping is not strictly required.
    assert "print(" in out
    assert "'hi'" in out or "&#39;hi&#39;" in out


def test_render_safe_renders_lists():
    out = render_safe("- one\n- two\n- three")
    assert out.count("<li>") == 3
    assert "<ul>" in out


def test_render_safe_renders_headings():
    out = render_safe("## Heading two\n\n### Heading three")
    assert "<h2>" in out
    assert "<h3>" in out


def test_render_safe_renders_blockquote():
    out = render_safe("> Quoted line")
    assert "<blockquote>" in out


def test_render_safe_renders_strikethrough():
    out = render_safe("~~old~~")
    assert "<s>" in out or "<del>" in out  # markdown-it emits <s>


def test_render_safe_table_supported():
    md = "| A | B |\n|---|---|\n| 1 | 2 |"
    out = render_safe(md)
    assert "<table>" in out
    assert "<th>" in out
    assert "<td>1</td>" in out


# --- Links --------------------------------------------------------------


def test_render_safe_renders_http_link_with_rel_noopener():
    out = render_safe("[link](https://example.com)")
    assert "<a" in out
    assert 'href="https://example.com"' in out
    assert "noopener" in out


def test_render_safe_strips_javascript_url():
    """`javascript:` URLs must NOT survive into an executable <a href>.
    Markdown-it rejects the link at parse time (scheme isn't valid for an
    autolink target) leaving the [bad](javascript:...) source as plain
    text. The text bytes survive but are inert — no anchor tag wraps them.
    """
    out = render_safe("[bad](javascript:alert(1))")
    # The dangerous bit is `<a href="javascript:..."` — verify no such
    # anchor is emitted. The literal text "javascript:" CAN appear as
    # plain text in the rendered paragraph; that's inert.
    assert '<a href="javascript:' not in out.lower()
    assert "<a " not in out  # no <a> tag at all means no clickable href


def test_render_safe_strips_data_url():
    """`data:` URLs also blocked (image-payload / phishing vector)."""
    out = render_safe("[x](data:text/html,test)")
    assert '<a href="data:' not in out.lower()
    assert "<a " not in out


def test_render_safe_allows_mailto():
    """mailto: stays in the allowlist for contact links in descriptions."""
    out = render_safe("[email](mailto:hi@example.com)")
    assert 'href="mailto:hi@example.com"' in out


# --- HTML injection ------------------------------------------------------


def test_render_safe_strips_raw_script_tag():
    """Curator pastes `<script>` literally in markdown source — markdown-it
    is configured with `html=False` so raw HTML is escaped, not parsed.
    nh3's second-pass sanitizes whatever escaping missed."""
    out = render_safe("Hello <script>alert(1)</script> world")
    assert "<script>" not in out
    assert "alert(1)" not in out or "&lt;script&gt;" in out


def test_render_safe_strips_iframe():
    """Iframes are not in this allowlist (the news sanitizer permits them
    for video providers; marketplace descriptions don't need them)."""
    out = render_safe('<iframe src="https://evil.example.com"></iframe>')
    assert "<iframe" not in out
    assert "evil.example.com" not in out or "&lt;iframe" in out


def test_render_safe_strips_event_handler_attribute():
    """`onerror=` on an <img> must NOT survive into the rendered HTML as
    an executable attribute. markdown-it with `html=False` already escapes
    the literal `<img ...>` to `&lt;img ...&gt;` text; verify no live
    `<img>` tag (with or without onerror) reaches output."""
    out = render_safe('<img src=x onerror=alert(1)>')
    # No live <img> tag — the raw HTML was escaped to text, the substring
    # "onerror" may appear inside escaped text but cannot fire.
    assert "<img" not in out  # raw open-tag would mean live attribute
    # The escaped form `&lt;img` is fine.
    assert "&lt;img" in out or "&amp;lt;img" in out
