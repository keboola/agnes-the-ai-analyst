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


# --- XSS regression — disallowed schemes via markdown-native links ----------
# CommonMark autolinks (e.g. `<javascript:alert(1)>`) and reference links
# emit `href` regardless of scheme; defense rests on nh3's `url_schemes`
# allowlist. These tests pin the scheme allowlist so adding `data:` /
# `tel:` / etc. later requires updating both the allowlist AND a test.


def test_render_safe_strips_javascript_autolink():
    """`<javascript:...>` autolink — with `html=False` markdown-it escapes
    the literal `<` to `&lt;`, so it never reaches the href emitter at all.
    Either way: no live `<a href="javascript:...">` anchor in output."""
    out = render_safe("<javascript:alert(1)>")
    assert 'href="javascript:' not in out.lower()
    assert "<a " not in out  # no anchor tag at all


def test_render_safe_strips_javascript_link_mixed_case():
    """Scheme matching must be case-insensitive (`JaVaScRiPt:` would slip
    through a literal-string filter). `javascript:` may appear as escaped
    text in the output; the invariant is that no live `<a href=...>`
    anchor was emitted."""
    out = render_safe("[click](JaVaScRiPt:alert(1))")
    assert 'href=' not in out  # link entirely stripped


def test_render_safe_strips_data_url_link():
    """`data:` URLs can carry `text/html` payloads — browsers happily
    execute scripts in them. Allowlist must reject."""
    out = render_safe("[click](data:text/html,<script>alert(1)</script>)")
    assert 'href="data:' not in out.lower()


def test_render_safe_strips_vbscript_link():
    """Legacy IE attack surface, still worth pinning."""
    out = render_safe("[click](vbscript:msgbox(1))")
    assert 'href="vbscript:' not in out.lower()


def test_render_safe_strips_javascript_reference_link():
    """Reference-style links route through the same href emitter."""
    out = render_safe("[click][1]\n\n[1]: javascript:alert(1)")
    assert 'href="javascript:' not in out.lower()


def test_render_safe_keeps_http_https_mailto_schemes():
    """Allowlist positive-coverage so future tightening is a visible diff."""
    out = render_safe("[a](https://example.com) [b](http://example.com) [c](mailto:x@example.com)")
    assert 'href="https://example.com"' in out
    assert 'href="http://example.com"' in out
    assert 'href="mailto:x@example.com"' in out


def test_render_safe_adds_noopener_noreferrer_rel():
    """Render must add `rel="noopener noreferrer"` to outbound links so
    `window.opener` tabnabbing isn't possible from curator-controlled
    markdown."""
    out = render_safe("[a](https://example.com)")
    assert "noopener" in out and "noreferrer" in out


# ---------------------------------------------------------------------------
# render_plain: plain-text projection for previews / filter indexes
# ---------------------------------------------------------------------------


def test_render_plain_strips_markdown_markup():
    from app.markdown_render import render_plain

    out = render_plain("**Bold** and `code` and *em*.")
    assert out == "Bold and code and em."


def test_render_plain_separates_block_boundaries():
    """Adjacent blocks must not fuse into one word when tags are stripped."""
    from app.markdown_render import render_plain

    out = render_plain("## Heading\n\nParagraph one.\n\n- item a\n- item b")
    assert "Heading Paragraph one." in out
    assert "item a item b" in out


def test_render_plain_unescapes_entities():
    from app.markdown_render import render_plain

    assert render_plain("a & b < c") == "a & b < c"


def test_render_plain_empty_and_none():
    from app.markdown_render import render_plain

    assert render_plain(None) == ""
    assert render_plain("") == ""


def test_render_plain_emits_no_tags():
    """Whatever the input (markdown, inline HTML, links), the output is
    tag-free text; it is injected with normal Jinja escaping, so any
    surviving `<` would render literally, but none should survive."""
    from app.markdown_render import render_plain

    out = render_plain("[x](https://example.com)\n\n| a | b |\n|---|---|\n| 1 | 2 |")
    assert "<" not in out
    assert "x" in out and "1 2" in out
