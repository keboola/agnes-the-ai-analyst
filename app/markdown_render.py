"""Safe markdown → HTML renderer for curator-authored marketplace content.

Two stages:

1. **Render** — `markdown-it-py` in CommonMark mode (no raw HTML pass-through,
   no autolink to javascript:, no unsafe blocks). Tables and strikethrough
   are enabled because they show up routinely in `long_description` /
   `sample_interaction.assistant`. Linkify is OFF — curators write explicit
   links; auto-linking bare strings adds attack surface without value here.
   Callers whose stored text may itself BE html rather than markdown pass
   `html_source=True` for a second renderer that does pass raw HTML through
   to stage 2 — see the `render_safe` docstring for when that is right.

2. **Sanitize** — funnel the rendered HTML through `nh3` (Rust-backed ammonia
   allowlist) so anything the renderer let through that we don't want
   reaching the browser (raw HTML the curator inlined, `javascript:` URLs,
   on*-handlers, unknown tags) gets stripped.

Used by `app/api/marketplace.py` to pre-render `description` and
`sample_interaction.assistant` from `marketplace-metadata.json` before the
HTML lands in `PluginDetailResponse`, and by the `/catalog/semantics` metric
rows (`html_source=True` there). The template injects with `{{ x | safe }}`
trusting the stored value — no second-pass sanitization on render.
"""

from __future__ import annotations

import html as html_lib
import re
from typing import Optional

import nh3
from markdown_it import MarkdownIt


# CommonMark-strict renderer. `html=False` disables inline raw HTML so a
# curator who pastes `<script>` inside markdown gets the literal string
# rendered, not an executable tag. `linkify` is off to keep bare strings
# from becoming clickable links.
_md = (
    MarkdownIt("commonmark", {"html": False, "linkify": False})
    .enable("table")
    .enable("strikethrough")
)

# Same renderer with raw HTML pass-through, for stored text that may itself
# BE html rather than markdown (`html_source=True` below). Safety still rests
# on the same nh3 allowlist — the difference is only whether a `<strong>` in
# the source becomes markup or becomes the visible characters `<strong>`.
_md_html_source = (
    MarkdownIt("commonmark", {"html": True, "linkify": False})
    .enable("table")
    .enable("strikethrough")
)


# nh3 allowlist — narrower than `src/sanitize_news.py` (which supports
# admin-edited HTML with iframes). Marketplace descriptions don't need
# iframes, images, or HTML5 details — just text formatting + links + code.
_ALLOWED_TAGS: set[str] = {
    "p", "br",
    "h2", "h3", "h4",
    "ul", "ol", "li",
    "strong", "em", "b", "i", "s",
    "code", "pre", "blockquote",
    "a",
    "table", "thead", "tbody", "tr", "th", "td",
    "hr",
}

_ALLOWED_ATTRIBUTES: dict[str, set[str]] = {
    # `rel` is managed via nh3's `link_rel` param; do NOT list here.
    "a": {"href", "title"},
    "th": {"align"},
    "td": {"align"},
}

_ALLOWED_URL_SCHEMES: set[str] = {"http", "https", "mailto"}


def render_safe(markdown: Optional[str], *, html_source: bool = False) -> str:
    """Render curator-authored markdown to sanitized HTML.

    Returns ``""`` for ``None`` or empty input. The output is safe to inject
    into a template with `{{ x | safe }}` — every attack surface markdown-it
    leaves open (raw `<script>`, `javascript:` URLs, event handlers) is
    stripped by nh3 before return.

    ``html_source=True`` for stored text whose dialect is not guaranteed to be
    markdown — a metric description imported verbatim from an external catalog
    sits in the same column as a hand-authored markdown one, and such catalogs
    routinely store rich HTML. Raw HTML then renders as markup instead of as
    its own escaped characters; the nh3 allowlist is identical either way, so
    this widens what is *displayed*, never what is *allowed*. Leave it off for
    curator-authored content, where pasted HTML showing up as literal text is
    the intended tell.
    """
    if not markdown:
        return ""
    html = (_md_html_source if html_source else _md).render(markdown)
    return nh3.clean(
        html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        url_schemes=_ALLOWED_URL_SCHEMES,
        link_rel="noopener noreferrer",
        strip_comments=True,
    )


# Closing tags of block-level elements (plus <br>) mark word boundaries when
# flattening rendered HTML to plain text; without this, "<p>a</p><p>b</p>"
# collapses to "ab".
_BLOCK_BOUNDARY_RE = re.compile(
    r"</(?:p|li|h[1-6]|tr|t[dh]|blockquote|pre)>|<br ?/?>"
)


def render_plain(markdown: Optional[str], *, html_source: bool = False) -> str:
    """Plain-text projection of ``render_safe`` output.

    For one-line previews and client-side filter indexes where markup,
    literal ``**`` / ``#`` as much as HTML tags, is noise. Pipeline:
    render + sanitize (``render_safe``), turn block boundaries into spaces,
    strip every remaining tag (nh3 with an empty allowlist), unescape
    entities back to text, collapse whitespace. The result is data, not
    HTML: inject with normal Jinja escaping, never ``| safe``.

    ``html_source`` carries the same meaning as in ``render_safe``, and this
    projection is why it exists: without it an HTML-blob input is escaped by
    the renderer into entities, so the tag-strip below finds no tags to
    remove and the closing ``unescape`` hands them back as visible ``<p>``
    / ``<strong>`` characters — markup surviving into the one place that
    promises none.
    """
    if not markdown:
        return ""
    html = _BLOCK_BOUNDARY_RE.sub(" ", render_safe(markdown, html_source=html_source))
    text = html_lib.unescape(nh3.clean(html, tags=set()))
    return " ".join(text.split())


__all__ = ["render_plain", "render_safe"]
