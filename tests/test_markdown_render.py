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


# ---------------------------------------------------------------------------
# html_source=True: the stored text may itself be an HTML blob
#
# OpenMetadata "stores descriptions as rich HTML" (connectors/openmetadata/
# transformer.py::strip_html says so), and those descriptions land verbatim in
# `metric_definitions.description` alongside hand-authored markdown from
# docs/metrics/*.yaml. One column, two dialects — so the metric surfaces opt
# into a renderer that accepts both.
# ---------------------------------------------------------------------------


HTML_BLOB = "<p><strong>Live Deals</strong> &mdash; deals currently live.</p>"


def test_render_plain_html_source_strips_tags_instead_of_resurrecting_them():
    """The reported bug: an HTML-blob description leaked `<p><strong>` as
    literal text into the one-line preview.

    Without `html_source`, the markdown renderer escapes the blob into
    entities, nh3's tag-strip finds no tags left to remove, and the closing
    `html.unescape` turns the entities back into visible characters — a
    plain-text projection containing markup, which is exactly what
    render_plain exists to prevent."""
    from app.markdown_render import render_plain

    out = render_plain(HTML_BLOB, html_source=True)
    assert out == "Live Deals — deals currently live."
    assert "<" not in out and ">" not in out


def test_render_safe_html_source_renders_the_blob_as_markup():
    """The detail view shows bold text, not the characters `<strong>`."""
    from app.markdown_render import render_safe

    out = render_safe(HTML_BLOB, html_source=True)
    assert "<strong>Live Deals</strong>" in out
    assert "&lt;strong&gt;" not in out


def test_html_source_still_renders_plain_markdown():
    """The same column also holds hand-authored markdown, so the permissive
    mode must not stop being a markdown renderer."""
    from app.markdown_render import render_plain, render_safe

    assert "<strong>hello</strong>" in render_safe("**hello** world", html_source=True)
    assert render_plain("**Bold** and `code`.", html_source=True) == "Bold and code."


def test_html_source_still_sanitizes():
    """Accepting HTML from the source is nh3's job to make safe, not a hole:
    the allowlist is unchanged, so script/handlers/unknown tags still go."""
    from app.markdown_render import render_plain, render_safe

    out = render_safe('<p onclick="steal()">hi</p><script>alert(1)</script>', html_source=True)
    assert "<script" not in out.lower()
    assert "onclick" not in out.lower()
    assert "alert(1)" not in out
    assert "hi" in out
    assert "alert(1)" not in render_plain("<script>alert(1)</script>", html_source=True)


def test_default_mode_is_unchanged_by_the_new_parameter():
    """Marketplace content keeps its stricter posture — a curator's pasted
    HTML renders as visible text there, deliberately."""
    from app.markdown_render import render_safe

    out = render_safe(HTML_BLOB)
    assert "<strong>" not in out
    assert "&lt;strong&gt;" in out


def test_html_source_eats_angle_bracketed_words_and_the_default_does_not():
    """The cost of the permissive renderer, pinned so it stays a decision.

    markdown-it reads `<shipped>` as an unknown tag, nh3's allowlist rejects
    it, and a pseudo-tag carries no child text — so the characters vanish
    rather than being escaped and shown. `<int>.` takes the rest of the line
    with it (unclosed tag). This is exactly why callers must not route every
    row through `html_source=True` on a hunch about the content: the flag is
    keyed on the writer (see `app.api.metrics.stores_html`)."""
    from app.markdown_render import render_plain

    prose = "Counts orders <shipped> per day; column type List<int>."
    assert render_plain(prose) == prose
    assert render_plain(prose, html_source=True) == "Counts orders per day; column type List"


def test_html_source_keeps_a_boundary_where_a_stripped_block_tag_was():
    """`<div>` is not on the render allowlist, so it is removed — but it was
    separating two lines, and a removal that leaves no boundary fuses them
    into "First line.Second line.". Boundaries are therefore inserted into the
    RENDERED html, before sanitization, not into `render_safe`'s output."""
    from app.markdown_render import render_plain

    out = render_plain("<div>First line.</div><div>Second line.</div>", html_source=True)
    assert out == "First line. Second line."


def test_plain_projection_still_drops_script_content_entirely():
    """Inserting boundaries pre-sanitization must not turn script bodies into
    visible text: the empty-allowlist clean removes those tags WITH their
    contents, which is stricter than the render allowlist, not weaker."""
    from app.markdown_render import render_plain

    assert render_plain("<script>alert(1)</script>visible", html_source=True) == "visible"


def test_html_source_keeps_div_structure_in_the_rendered_output():
    """The render-side twin of the boundary bug. `render_plain` recovers a
    space where a stripped <div> was, but the DETAIL view renders markup — so
    stripping the div there fused the two lines it separated into
    "sales.Excludes refunds". Structural containers are kept on this path."""
    from app.markdown_render import render_safe

    out = render_safe(
        "<div>Share of qualified sales.</div><div>Excludes refunds.</div>", html_source=True
    )
    assert out == "<div>Share of qualified sales.</div><div>Excludes refunds.</div>"


def test_html_source_extra_tags_carry_no_attributes():
    """They are laid out, never wired: no _ALLOWED_ATTRIBUTES entry exists for
    them, so handlers and everything else go."""
    from app.markdown_render import render_safe

    out = render_safe('<div onclick="steal()" class="x" id="y">hi</div>', html_source=True)
    assert out == "<div>hi</div>"


def test_default_path_still_refuses_div():
    """The widened set is scoped to html_source — curator markdown keeps the
    narrow allowlist, where a pasted <div> stays visible text."""
    from app.markdown_render import render_safe

    assert "<div>" not in render_safe("<div>a</div>")


def test_boundary_and_allowlist_agree_on_what_a_block_container_is():
    """Guard against the two lists drifting apart.

    `_BLOCK_BOUNDARY_RE` decides what leaves a space behind in the plain-text
    projection; `_HTML_SOURCE_EXTRA_TAGS` decides what keeps its structure in
    the rendered detail. A tag treated as a block separator by one and dropped
    by the other fuses the lines it separated — which is how figure/figcaption
    slipped through the first version of this fix."""
    import re

    from app.markdown_render import _ALLOWED_TAGS, _BLOCK_TAGS, _HTML_SOURCE_EXTRA_TAGS

    named = set()
    for alt in re.split(r"\|", _BLOCK_TAGS):
        if re.fullmatch(r"[a-z]+", alt):
            named.add(alt)
        elif m := re.fullmatch(r"([a-z]+)\[(\d)-(\d)\]", alt):  # h[1-6]
            named |= {f"{m[1]}{d}" for d in range(int(m[2]), int(m[3]) + 1)}
        elif m := re.fullmatch(r"([a-z]+)\[([a-z]+)\]", alt):  # t[dh]
            named |= {f"{m[1]}{c}" for c in m[2]}
    separators_not_rendered = named - _ALLOWED_TAGS - _HTML_SOURCE_EXTRA_TAGS
    assert not separators_not_rendered, (
        f"treated as a block separator but stripped with no structure: {sorted(separators_not_rendered)}"
    )


def test_nested_structure_separated_by_an_opening_tag_does_not_fuse():
    """`<figure>A<figcaption>B` has no CLOSING tag between A and B, so a
    close-only boundary pattern fused them."""
    from app.markdown_render import render_plain

    out = render_plain(
        "<figure>Chart of live deals.<figcaption>Measured daily.</figcaption></figure>", html_source=True
    )
    assert out == "Chart of live deals. Measured daily."
    assert render_plain("<div>Line one<div>Line two</div></div>", html_source=True) == "Line one Line two"


def test_br_is_still_a_boundary():
    """It is the one non-container in the pattern; a rewrite of the tag
    alternation must not drop it."""
    from app.markdown_render import render_plain

    assert render_plain("a<br>b", html_source=True) == "a b"
    assert render_plain("a<br/>b", html_source=True) == "a b"


def test_a_quoted_gt_inside_a_layout_tag_does_not_leak():
    """`>` inside a quoted attribute value does not close the tag, so a
    `[^>]*>` attribute run cut `<div title="a>b">` in half and left `b">` as
    visible characters in the preview."""
    from app.markdown_render import render_plain

    assert render_plain('<div title="a>b">First.</div><div>Second.</div>', html_source=True) == "First. Second."
    assert render_plain("<div title='a>b'>First.</div><div>Second.</div>", html_source=True) == "First. Second."


def test_boundary_pattern_stays_linear_on_unclosed_tags():
    """Two separate hazards in one pattern, so two shapes of hostile input.

    ONE unterminated tag with many quoted runs is the exponential-backtracking
    shape (alternation inside a star). MANY unterminated tags is the quadratic
    one: each restarts a scan over the remainder unless the bare branch stops
    at the next `<`. The second is what actually bit — 20 KB of `<div ` took
    620 ms before the `<` exclusion — so it is measured against a growth ratio
    rather than a wall-clock threshold that a slow runner could trip."""
    import time

    from app.markdown_render import _BLOCK_BOUNDARY_RE, render_plain

    started = time.perf_counter()
    render_plain("<div " + '"x"' * 4000, html_source=True)
    assert time.perf_counter() - started < 1.0

    def elapsed(n: int) -> float:
        text = "<div " * n
        started = time.perf_counter()
        _BLOCK_BOUNDARY_RE.sub(" ", text)
        return time.perf_counter() - started

    elapsed(2000)  # warm up, so the first call does not carry setup cost
    small, large = elapsed(2000), elapsed(8000)
    # Linear would be ~4x for 4x the input; quadratic ~16x. Generous ceiling —
    # the point is to catch the class, not to benchmark the runner.
    assert large < max(small * 8, 0.05), f"superlinear growth: {small=:.4f} {large=:.4f}"
